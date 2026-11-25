from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError


COST_CATEGORIES = [
    ('purchase', 'Purchases and Transport'), ('inventory', 'Stock and MO Materials'),
    ('fabrication', 'Workshop Overhead'), ('labour', 'Labour'),
    ('expenses', 'Posted Staff Expenses'), ('landed', 'Direct Landed Charges'), ('other', 'Other Actual Costs'),
]


class ProjectBudgetLine(models.Model):
    _inherit = 'budget.line'

    acme_cost_category = fields.Selection(COST_CATEGORIES)


class ProjectCostingCompany(models.Model):
    _inherit = 'res.company'

    def action_acme_cost_issue_types(self):
        self.ensure_one()
        self.check_access('read')
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_('Only Accounting administrators can open project costing setup.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Stock Operation Types — Analytic Costs'),
            'res_model': 'stock.picking.type', 'view_mode': 'list,form',
            'domain': [('company_id', '=', self.id), ('code', 'in', ['internal', 'outgoing'])],
        }


class ProjectCosting(models.Model):
    _inherit = 'project.project'

    acme_operating_budget_id = fields.Many2one(
        'budget.analytic', string='Approved Operations Budget', check_company=True, ondelete='restrict',
        domain="[('company_id', '=', company_id), ('budget_type', '=', 'expense'), ('state', 'in', ['confirmed', 'done'])]",
    )

    @api.constrains('acme_operating_budget_id', 'company_id', 'account_id')
    def _check_acme_operating_budget(self):
        for project in self.filtered('acme_operating_budget_id'):
            budget = project.acme_operating_budget_id
            account = project.account_id
            if (not account or budget.company_id != project.company_id or budget.state not in ('confirmed', 'done')
                    or budget.budget_type != 'expense'
                    or not budget.budget_line_ids.filtered(lambda line: line[account.plan_id._column_name()] == account)):
                raise ValidationError(_('Choose an approved expense budget that contains this project analytic account.'))

    def _acme_can_view_financials(self):
        self.ensure_one()
        if self.company_id not in self.env.companies or not self.has_access('read'):
            return False
        return self.user_id == self.env.user or self._acme_has_financial_role()

    @api.model
    def _acme_has_financial_role(self):
        return any(self.env.user.has_group(group) for group in (
            'acme_project_core.group_acme_odm', 'acme_project_core.group_acme_dm',
            'acme_project_core.group_acme_dgm', 'account.group_account_invoice', 'account.group_account_readonly',
        ))

    def _show_profitability_helper(self):
        self.ensure_one()
        if self.acme_boq_initialized and not self._acme_can_view_financials():
            return False
        return super()._show_profitability_helper()

    def _acme_cost_inputs(self, date_to):
        self.ensure_one()
        Analytic = self.env['account.analytic.line']
        column = self.account_id.plan_id._column_name()
        lines = Analytic.search([(column, '=', self.account_id.id), ('company_id', '=', self.company_id.id),
                                 ('date', '<=', date_to)])
        overhead = employee_cost = duplicate = self.env['account.analytic.line']
        issues = set()
        if 'mrp.workorder' in self.env and 'mo_analytic_account_line_ids' in self.env['mrp.workorder']._fields:
            Order = self.env['mrp.workorder']
            if Order.has_access('read'):
                workorders = Order.search([('production_id.project_id', '=', self.id)])
                overhead = workorders.mo_analytic_account_line_ids
                duplicate = workorders.wc_analytic_account_line_ids - overhead
                if 'employee_analytic_account_line_ids' in Order._fields:
                    employee_cost = workorders.employee_analytic_account_line_ids
                    logs = workorders.time_ids.filtered(lambda log: log.employee_id and log.duration > 0)
                    if logs.filtered(lambda log: log.employee_cost <= 0):
                        issues.add(_('Some workshop work logs have no employee cost.'))
            elif lines.filtered(lambda line: line.category == 'manufacturing_order'):
                issues.add(_('Workshop source access is required to separate material and labour costs.'))
        if self.company_id.acme_separate_workshop_time and employee_cost:
            project_cost = employee_cost & lines
            duplicate |= lines.filtered(lambda line: line.project_id == self and line.acme_workshop_time_id
                and bool(line.acme_workshop_time_id.workorder_id.employee_analytic_account_line_ids.filtered(
                    lambda entry: entry in project_cost and entry.employee_id == line.employee_id and entry.amount
                )))
        if self.company_id.acme_material_cost_basis == 'consumption':
            duplicate |= self._acme_excluded_stock_cost_lines(lines, date_to, issues)
        return lines - duplicate, set(overhead.ids), set(employee_cost.ids), issues

    def _acme_excluded_stock_cost_lines(self, lines, cutoff, issues):
        stock_lines = lines.filtered(lambda line: not line.move_line_id and line.category == 'picking_entry')
        Move = self.env['stock.move']
        if not Move.has_access('read'):
            if stock_lines:
                issues.add(_('Stock movement access is required to verify chargeable issue costs.'))
            return stock_lines
        moves = Move.search([
            ('picking_id.project_id', '=', self.id), ('company_id', '=', self.company_id.id),
            ('state', '=', 'done'), ('date', '<', fields.Datetime.to_datetime(cutoff) + timedelta(days=1)),
        ])
        staging = moves.filtered('acme_boq_demand_id') if 'acme_boq_demand_id' in Move._fields else Move
        eligible_moves = (moves - staging).filtered(
            lambda move: move.picking_type_id.code in ('internal', 'outgoing') and move.product_id.is_storable
            and not move.raw_material_production_id and not move.move_dest_ids.raw_material_production_id
        )
        chargeable = eligible_moves.filtered(lambda move: move.picking_type_id.analytic_costs)
        tracked = moves.analytic_account_line_ids & stock_lines
        if stock_lines - tracked:
            issues.add(_('Some analytic stock costs have no readable source stock movement.'))
        if eligible_moves.filtered(lambda move: move.picking_type_id.code == 'outgoing') - chargeable:
            issues.add(_('Confirm Analytic Costs on the operation types for completed project issues.'))
        if chargeable.filtered(lambda move: move.quantity and not move.analytic_account_line_ids):
            issues.add(_('Some chargeable project issues lack native analytic valuation entries.'))
        return stock_lines - chargeable.analytic_account_line_ids

    def _acme_cost_category(self, line, overhead, employee_cost, issues):
        bill_line = line.move_line_id
        method = self.company_id.acme_material_cost_basis
        if bill_line:
            if bill_line.move_id.state != 'posted' or bill_line.account_id.account_type not in (
                'expense', 'expense_direct_cost', 'expense_depreciation',
            ):
                return False
            move = bill_line.move_id
            while move and not move.acme_settlement_advance_id:
                move = move.reversed_entry_id
            if move:
                return False
            if bill_line.expense_id:
                return 'expenses'
            landed = bool(getattr(bill_line, 'is_landed_costs_line', False))
            inventory = landed or bool(bill_line.product_id.is_storable)
            if inventory and not method:
                issues.add(_('Finance must select a project inventory cost basis.'))
                return False
            if inventory and method == 'consumption':
                return False
            if landed:
                return 'landed'
            if bill_line.move_id.move_type in ('in_invoice', 'in_refund', 'in_receipt'):
                return 'purchase'
            # A posted stock valuation expense is an alternative representation of inventory usage.
            if getattr(bill_line.move_id, 'stock_move_ids', False) or bill_line.display_type == 'cogs':
                return False
            return 'other'
        if line.id in overhead:
            return 'fabrication'
        if line.id in employee_cost:
            if not self.company_id.acme_separate_workshop_time:
                issues.add(_('Confirm which work-order hours are excluded from project timesheets.'))
                return False
            return 'labour'
        if line.category in ('manufacturing_order', 'picking_entry'):
            if not method:
                issues.add(_('Finance must select a project inventory cost basis.'))
            return 'inventory' if method == 'consumption' else False
        if getattr(line, 'employee_id', False) and line.unit_amount and getattr(line, 'project_id', False) == self:
            if not line.amount:
                issues.add(_('Some project timesheets have no recorded employee cost.'))
            return 'labour'
        return False

    def _acme_approved_cost_budget(self, date_to):
        if not self.account_id:
            return False, {}
        column = self.account_id.plan_id._column_name()
        source = self.env['budget.line'].search([
            (column, '=', self.account_id.id), ('company_id', '=', self.company_id.id),
            ('budget_analytic_id.budget_type', '=', 'expense'), ('budget_analytic_state', 'in', ['confirmed', 'done']),
        ])
        budget = self.acme_operating_budget_id or source.budget_analytic_id
        if len(budget) != 1:
            return False, {}
        budget_lines = source.filtered(lambda line: line.budget_analytic_id == budget)
        if not budget_lines:
            return False, {}
        categories = defaultdict(float)
        total = 0
        for line in budget_lines:
            amount = line.currency_id._convert(line.budget_amount, self.acme_contract_currency_id, self.company_id, date_to)
            total += amount
            if line.acme_cost_category:
                categories[line.acme_cost_category] += amount
        return total, dict(categories)

    def _acme_commercial_snapshot(self, date_to):
        currency = self.acme_contract_currency_id
        addenda = self.env['sale.order'].search([
            ('project_id', '=', self.id), ('acme_original_contract_id', '=', self.acme_order_id.id),
            ('acme_addendum_variation_id', '!=', False), ('state', '=', 'sale'),
            ('date_order', '<', fields.Datetime.to_datetime(date_to) + timedelta(days=1)),
        ])
        commercial_delta = 0
        represented = set()
        end = fields.Datetime.to_datetime(date_to) + timedelta(days=1)
        for revision in self.acme_boq_history_ids.filtered(
            lambda item: item.state == 'approved' and item.decision_at and item.decision_at < end
        ):
            for change in revision.changes:
                before, after = change['before'], change['after']
                if 'selling_rate' in before or 'selling_rate' in after:
                    represented.add(revision.id)
                    commercial_delta += (after.get('quantity', 0) * after.get('selling_rate', 0)
                                         - before.get('quantity', 0) * before.get('selling_rate', 0))
        addenda = addenda.filtered(lambda order: order.acme_addendum_variation_id.acme_variation_revision_id.id
                                  not in represented)
        contract = self.acme_contract_amount + commercial_delta + sum(order.currency_id._convert(
            order.amount_untaxed, currency, self.company_id, fields.Date.to_date(order.date_order),
        ) for order in addenda)
        certificates = self.env['acme.project.certificate'].search([
            ('project_id', '=', self.id), ('acme_certificate_type', '=', 'progress'), ('state', '=', 'certified'),
            ('date', '<=', date_to),
        ])
        eligible = certificates.filtered(lambda record: record.acme_pc_snapshot and record.invoice_id)
        originals = eligible.invoice_id.filtered(
            lambda move: move.move_type == 'out_invoice' and move.state == 'posted' and move.date <= date_to
        )
        credits = originals.reversal_move_ids.filtered(
            lambda move: move.state == 'posted' and move.move_type == 'out_refund' and move.date <= date_to
        )
        billed = sum(move.currency_id._convert(
            move.amount_untaxed, currency, self.company_id, move.date,
        ) * (-1 if move.move_type == 'out_refund' else 1) for move in originals | credits)
        pending = sum(certificates.filtered(lambda cert: cert.acme_pc_snapshot
                                            and cert.invoice_id.state not in ('posted', 'cancel')).mapped('work_amount'))
        return contract, billed, pending, eligible, bool(certificates - eligible)

    def _acme_costing_snapshot(self, date_to=None, date_from=None, period_date_to=None):
        self.ensure_one()
        if not self._acme_can_view_financials():
            raise AccessError(_('Project financials are restricted to authorized project and Finance users.'))
        date_to = date_to or fields.Date.context_today(self)
        currency = self.acme_contract_currency_id
        if not self.account_id or not currency:
            return {'issues': [_('The project analytic account and contract currency are required.')]}
        lines, overhead, employee_cost, issues = self._acme_cost_inputs(date_to)
        costs = defaultdict(float)
        period_cost = 0
        for line in lines:
            category = self._acme_cost_category(line, overhead, employee_cost, issues)
            if category:
                cost = -line.currency_id._convert(line.amount, currency, self.company_id, line.date)
                costs[category] += cost
                if (not date_from or line.date >= date_from) and (not period_date_to or line.date <= period_date_to):
                    period_cost += cost
        actual = sum(costs.values())
        cost_issues = sorted(issues)
        try:
            contract, billed, pending, eligible, legacy = self._acme_commercial_snapshot(date_to)
        except AccessError:
            if not period_date_to:
                raise
            contract = billed = pending = None
            eligible = self.env['acme.project.certificate']
            legacy = True
        if legacy:
            issues.add(_('Prior certificates without linked progress invoice figures require Finance reconciliation.'))
        try:
            budget, budgets = self._acme_approved_cost_budget(date_to)
        except AccessError:
            budget, budgets = False, {}
        update = self.env['project.update'].search([('project_id', '=', self.id), ('date', '<=', date_to)],
                                                   order='date desc,id desc', limit=1)
        progress = update.progress / 100 if update and 0 <= update.progress <= 100 else None
        method = self.company_id.acme_earned_revenue_method
        earned = None
        if method == 'physical' and progress is not None and contract and contract > 0:
            earned = contract * progress
        elif method == 'cost_to_cost' and not issues and budget and budget > 0 and contract and contract > 0:
            earned = contract * min(1, max(0, actual / budget))
        elif method == 'measured' and eligible and not legacy:
            earned = sum(eligible.mapped('work_amount'))
        return {
            'issues': sorted(issues), 'cost_issues': cost_issues, 'commercial_incomplete': legacy,
            'contract': contract, 'billed': billed, 'pending': pending,
            'actual': actual, 'period_cost': period_cost, 'costs': dict(costs),
            'budget': budget, 'budgets': budgets,
            'earned': earned, 'wip': earned - billed if earned is not None and billed is not None and not legacy else None,
            'progress_variance': budget * progress - actual if budget and progress is not None and not issues else None,
        }

    def _get_profitability_items(self, with_action=True):
        self.ensure_one()
        if not self.acme_boq_initialized:
            return super()._get_profitability_items(with_action)
        result = {'revenues': {'data': [], 'total': {'invoiced': 0.0, 'to_invoice': 0.0}},
                  'costs': {'data': [], 'total': {'billed': 0.0, 'to_bill': 0.0}}}
        if not self._acme_can_view_financials():
            return result
        try:
            data = self._acme_costing_snapshot()
        except AccessError:
            return result
        if 'billed' not in data:
            return result
        convert = lambda amount: self.acme_contract_currency_id._convert(amount, self.currency_id, self.company_id)
        result['revenues'] = {
            'data': [{'id': 'acme_progress_revenue', 'sequence': 10,
                      'invoiced': convert(data['billed']), 'to_invoice': convert(data['pending'])}],
            'total': {'invoiced': convert(data['billed']), 'to_invoice': convert(data['pending'])},
        }
        if not data['issues']:
            result['costs'] = {
                'data': [{'id': 'acme_actual_cost', 'sequence': 10, 'billed': -convert(data['actual']), 'to_bill': 0}],
                'total': {'billed': -convert(data['actual']), 'to_bill': 0},
            }
        return result

    def _get_profitability_labels(self):
        labels = super()._get_profitability_labels()
        labels.update(acme_progress_revenue=_('Posted Progress Revenue'), acme_actual_cost=_('Eligible Actual Costs'))
        return labels


class WorkshopTimesheetOverlap(models.Model):
    _inherit = 'account.analytic.line'

    acme_workshop_time_id = fields.Many2one(
        'mrp.workcenter.productivity', string='Matching Workshop Time',
        check_company=True, ondelete='restrict', copy=False,
        help='Link the workshop time only if this timesheet records the same work by the same employee.',
    )
    _acme_workshop_timesheet_unique = models.Constraint(
        'UNIQUE(acme_workshop_time_id)', 'A workshop time entry can be matched to only one timesheet.',
    )

    @api.constrains('acme_workshop_time_id', 'project_id', 'employee_id', 'unit_amount')
    def _check_acme_workshop_timesheet(self):
        for line in self.filtered('acme_workshop_time_id'):
            log = line.acme_workshop_time_id
            if (not line.project_id or line.project_id != log.workorder_id.production_id.project_id
                    or not line.employee_id or line.employee_id != log.employee_id
                    or abs(line.unit_amount - log.duration / 60) > 0.01):
                raise ValidationError(_('Match a workshop log for the same project, employee and hours.'))
