from collections import Counter, defaultdict
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools.misc import format_amount


def table(headers, rows):
    heading = Markup('').join(Markup('<th>%s</th>') % value for value in headers)
    body = Markup('').join(
        Markup('<tr>%s</tr>') % Markup('').join(Markup('<td>%s</td>') % value for value in row)
        for row in rows
    )
    return Markup('<table class="table table-sm"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>') % (heading, body)


class ProjectMonitoringDashboard(models.TransientModel):
    _inherit = 'acme.project.dashboard'

    acme_r13_summary = fields.Html(compute='_compute_monthly_monitoring')
    acme_cash_interval = fields.Selection([('week', 'Weekly'), ('month', 'Monthly')], default='week', required=True)
    acme_cash_from = fields.Date(string='Forecast From',
                                default=lambda self: fields.Date.context_today(self).replace(day=1))
    acme_cash_to = fields.Date(string='Forecast To',
                              default=lambda self: fields.Date.context_today(self) + timedelta(days=30))
    acme_r15_forecast = fields.Html(compute='_compute_cash_forecast')
    acme_r11_settlement = fields.Html(compute='_compute_r11_settlement')
    acme_show_portfolio = fields.Boolean(string='DM / DGM Consolidation')
    acme_portfolio_html = fields.Html(compute='_compute_portfolio')

    @api.depends('project_id')
    def _compute_r11_settlement(self):
        for dashboard in self:
            project = dashboard.project_id
            dashboard.acme_r11_settlement = False
            if not project or not project._acme_can_view_financials():
                continue
            requests = self.env['approval.request'].search([
                ('acme_project_id', '=', project.id), ('acme_kind', 'in', ['work_advance', 'petty_cash']), ('request_status', '=', 'approved'),
            ])
            rows = []
            for request in requests:
                amounts = [request[f'acme_settlement_{name}'] for name in (
                    'paid', 'applied', 'refunded', 'reimbursed', 'outstanding', 'excess_due',
                )]
                if request.acme_settlement_available:
                    if not request.acme_settlement_outstanding and not request.acme_settlement_excess_due and request.acme_settlement_paid:
                        continue
                    amounts = [dashboard._acme_monitoring_amount(amount, request.acme_settlement_company_currency_id)
                               for amount in amounts]
                else:
                    amounts = [_('Unavailable')] * len(amounts)
                rows.append((request.display_name, request.category_id.name, (request.acme_staff_employee_id.display_name or request.request_owner_id.display_name),
                             request.acme_settlement_due or _('Unset'), *amounts))
            dashboard.acme_r11_settlement = table(
                [_('Open Authorization'), _('Request Type'), _('Employee'), _('Due'), _('Paid'), _('Applied'), _('Refunded'),
                 _('Reimbursed'), _('Outstanding'), _('Excess Reimbursement Due')], rows,
            ) if rows else _('No visible open staff advances.')

    def _acme_status_update(self, project):
        return self.env['project.update'].search([
            ('project_id', '=', project.id), ('date', '<=', self.date_to),
        ], order='date desc, id desc', limit=1)

    def _acme_monitoring_amount(self, amount, currency=None):
        currency = currency or self.currency_id
        return format_amount(self.env, amount, currency) if currency else _('Unavailable: currency missing')

    @api.depends('project_id', 'date_from', 'date_to')
    def _compute_monthly_monitoring(self):
        for dashboard in self:
            project = dashboard.project_id
            dashboard.acme_r13_summary = False
            if not project or not dashboard.date_to:
                continue
            project.check_access('read')
            update = dashboard._acme_status_update(project)
            status = dict(update.fields_get(['status'])['status']['selection']).get(update.status) if update else False
            progress = ('%s%%' % update.progress) if update else _('Unavailable: no dated update')
            variance = ('%+.1f pp' % (update.progress - update.acme_planned_progress)) if update.acme_has_planned_progress else _('Unavailable: no planned progress')
            tasks = self.env['project.task'].search([
                ('project_id', '=', project.id), ('is_template', '=', False), ('state', '!=', '1_canceled'),
            ])
            parent_ids = set(tasks.parent_id.ids)
            late = Counter()
            for task in tasks:
                due = task.acme_end_date or fields.Date.to_date(task.date_deadline)
                if task.id in parent_ids or not due or not dashboard.date_from <= due <= dashboard.date_to:
                    continue
                completed = fields.Date.to_date(task.date_end)
                if ((completed and min(completed, dashboard.date_to) > due)
                        or (task.state != '1_done' and due < dashboard.date_to)):
                    late[task.acme_delay_reason] += 1
            labels = dict(tasks.fields_get(['acme_delay_reason'])['acme_delay_reason']['selection'])
            delay = ', '.join('%s: %s' % (labels.get(reason, _('Unrecorded')), count) for reason, count in late.items())
            certified = project.acme_certificate_ids.filtered(lambda certificate: certificate.state == 'certified')
            order = project.acme_order_id
            has_sales_access = bool(order and order.has_access('read'))
            rows = [
                (_('Project status'), status or _('Unavailable: no update')),
                (_('Reported progress'), progress),
                (_('Schedule variance'), variance),
                (_('Contract order amount to invoice (native)'), dashboard._acme_monitoring_amount(order.amount_to_invoice)
                 if has_sales_access else _('Unavailable: no accessible contract order')),
                (_('Contract order invoiced (native)'), dashboard._acme_monitoring_amount(order.amount_invoiced)
                 if has_sales_access else _('Unavailable: no accessible contract order')),
                (_('Billing against plan'), _('Unavailable: no approved time-phased billing baseline')),
                (_('Delays by reason for tasks due in the period'), delay or _('No recorded late tasks due in the period')),
                (_('Customer advance balance (untaxed)'), dashboard._acme_monitoring_amount(project.acme_advance_balance)),
                (_('Certified retention to date'), dashboard._acme_monitoring_amount(sum(certified.mapped('retention_amount')))),
                (_('Outstanding retention after releases'), _('Unavailable: no retention-release ledger linked to certificates')),
            ]
            try:
                costing = project._acme_costing_snapshot(dashboard.date_to, dashboard.date_from)
            except AccessError:
                costing = {}
            if 'billed' in costing:
                from .costing import COST_CATEGORIES

                def amount(value):
                    return dashboard._acme_monitoring_amount(value) if value is not None else _('Unavailable')

                rows += [
                    (_('Contract including confirmed addenda (untaxed)'), amount(costing['contract'])),
                    (_('Posted progress revenue net of linked credits (untaxed)'), amount(costing['billed'])),
                    (_('Authorized progress awaiting invoice posting'), amount(costing['pending'])),
                    (_('Eligible actual cost'), amount(costing['actual'] if not costing['issues'] else None)),
                    (_('Invoiced margin'), amount(costing['billed'] - costing['actual'] if not costing['issues'] else None)),
                    (_('Earned revenue (untaxed)'), amount(costing['earned'])),
                    (_('Signed WIP: earned less invoiced'), amount(costing['wip'])),
                    (_('Cost variance at reported progress'), amount(costing['progress_variance'])),
                    (_('Cost data gaps'), ' '.join(costing['issues']) or _('None identified')),
                ]
                breakdown = []
                for key, label in COST_CATEGORIES:
                    actual = costing['costs'].get(key, 0)
                    budget = costing['budgets'].get(key)
                    breakdown.append((_(label), amount(actual), amount(budget),
                                      '%.1f%%' % (100 * actual / budget) if budget and not costing['issues'] else _('Unavailable'),
                                      amount(budget - actual if budget is not None and not costing['issues'] else None)))
                dashboard.acme_r13_summary = (
                    table([_('R-13 metric'), _('Value')], rows)
                    + table([_('Cost Category'), _('Recorded Actual'), _('Approved Budget'),
                             _('Budget Used'), _('Budget Less Actual')], breakdown)
                )
                continue
            rows.append((_('Financial costing'), _('Unavailable: check access, analytic account, and contract currency')))
            dashboard.acme_r13_summary = table([_('R-13 metric'), _('Value')], rows)

    def _acme_cash_moves(self, project):
        Move = self.env['account.move'].with_company(project.company_id)
        order = project.acme_order_id
        customer = Move.search([
            ('company_id', '=', project.company_id.id), ('state', '=', 'posted'),
            ('move_type', 'in', ['out_invoice', 'out_refund']),
            '|', ('acme_project_id', '=', project.id), ('id', 'in', order.invoice_ids.ids),
        ])
        purchase_lines = self.env['purchase.order.line'].search([
            ('company_id', '=', project.company_id.id), ('order_id.project_id', '=', project.id),
            ('order_id.state', 'in', ['purchase', 'done']), ('display_type', '=', False),
        ])
        linked = purchase_lines.invoice_lines.move_id
        if project.account_id:
            linked |= self.env['account.move.line'].search([
                ('company_id', '=', project.company_id.id),
                ('move_id.move_type', 'in', ['in_invoice', 'in_refund']),
                ('analytic_distribution', 'in', project.account_id.ids),
            ]).move_id
        vendor = linked.filtered(lambda move: move.state == 'posted' and move.move_type in ('in_invoice', 'in_refund'))
        return customer, vendor, purchase_lines

    @api.depends('project_id', 'acme_cash_from', 'acme_cash_to', 'acme_cash_interval')
    def _compute_cash_forecast(self):
        for dashboard in self:
            dashboard.acme_r15_forecast = False
            project = dashboard.project_id
            if not project or not dashboard.acme_cash_from or not dashboard.acme_cash_to:
                continue
            project._acme_check_dashboard_access()
            if dashboard.acme_cash_from > dashboard.acme_cash_to:
                dashboard.acme_r15_forecast = _('Forecast dates must be in chronological order.')
                continue
            currency = dashboard.currency_id
            if not currency:
                dashboard.acme_r15_forecast = _('Unavailable: set the project contract currency.')
                continue
            try:
                customers, vendors, purchase_lines = dashboard._acme_cash_moves(project)
                report = dashboard._acme_cash_buckets(customers, vendors, purchase_lines)
            except AccessError:
                dashboard.acme_r15_forecast = _('Unavailable: access to invoices, bills, or purchase commitments is missing.')
                continue
            if report:
                dashboard.acme_r15_forecast = table(
                    [_('Period'), _('Customer Receipts'), _('Vendor Payments'), _('Unbilled PO Commitments'), _('Net')],
                    [(period, *(dashboard._acme_monitoring_amount(value) for value in
                                 (receipt, bill, commitment, receipt - bill - commitment)))
                     for period, (receipt, bill, commitment) in sorted(report.items())],
                )
            else:
                dashboard.acme_r15_forecast = _('No visible open invoices, bills, or unbilled commitments in this period.')

    def _acme_cash_buckets(self, customers, vendors, purchase_lines):
        self.ensure_one()
        rows = defaultdict(lambda: [0.0, 0.0, 0.0])
        today = fields.Date.context_today(self)
        currency = self.currency_id
        company = self.company_id

        def add(date, amount, source_currency, index):
            if not date or date > self.acme_cash_to:
                return
            if date < self.acme_cash_from:
                key = _('Before selected period')
            elif self.acme_cash_interval == 'month':
                key = date.strftime('%Y-%m')
            else:
                key = str(date - timedelta(days=date.weekday()))
            rows[key][index] += source_currency._convert(amount, currency, company, today)

        for invoices, index in ((customers, 0), (vendors, 1)):
            for move in invoices:
                sign = -1 if move.move_type.endswith('_refund') else 1
                share = self._acme_invoice_project_share(move)
                terms = move.line_ids.filtered(lambda line: line.account_id.account_type in ('asset_receivable', 'liability_payable'))
                for term in terms:
                    amount = abs(term.amount_residual_currency)
                    source_currency = term.currency_id or company.currency_id
                    if not source_currency.is_zero(amount):
                        add(term.date_maturity or move.invoice_date_due or move.date, sign * amount * share, source_currency, index)
        for line in purchase_lines.filtered(lambda record: record.product_qty > 0):
            billed = sum(
                (1 if bill.move_id.move_type == 'in_invoice' else -1)
                * (bill.product_uom_id or line.product_uom_id)._compute_quantity(bill.quantity, line.product_uom_id, round=False)
                for bill in line.invoice_lines
                if bill.move_id.state == 'posted' and bill.move_id.move_type in ('in_invoice', 'in_refund')
            )
            open_quantity = max(0, line.product_qty - max(0, billed))
            if open_quantity:
                add(fields.Date.to_date(line.date_planned), line.price_total * open_quantity / line.product_qty,
                    line.currency_id, 2)
        return rows

    def _acme_invoice_project_share(self, move):
        project = self.project_id
        if move.acme_project_id == project:
            return 1
        lines = move.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
        total = sum(abs(line.price_total) for line in lines)
        if not total:
            return 0
        owned = 0
        account_id = str(project.account_id.id)
        if move.is_sale_document() and project.acme_order_id:
            for line in lines:
                orders = line.sale_line_ids.order_id
                if orders == project.acme_order_id:
                    owned += abs(line.price_total)
            return owned / total
        for line in lines:
            if line.purchase_line_id.order_id.project_id == project:
                owned += abs(line.price_total)
            elif project.account_id:
                share = sum(percent for accounts, percent in (line.analytic_distribution or {}).items()
                            if account_id in accounts.split(','))
                owned += abs(line.price_total) * min(100, max(0, share)) / 100
        return min(1, owned / total)

    @api.depends('acme_show_portfolio', 'date_from', 'date_to')
    def _compute_portfolio(self):
        for dashboard in self:
            dashboard.acme_portfolio_html = False
            if not dashboard.acme_show_portfolio:
                continue
            if not any(self.env.user.has_group(group) for group in (
                'acme_project_core.group_acme_dm', 'acme_project_core.group_acme_dgm',
            )):
                raise AccessError(_('Only DM and DGM Operations may open the consolidation.'))
            projects = self.env['project.project'].search([
                ('company_id', 'in', self.env.companies.ids), ('acme_boq_initialized', '=', True),
                ('is_template', '=', False),
            ])
            rows = []
            for project in projects:
                update = dashboard._acme_status_update(project)
                status = dict(update.fields_get(['status'])['status']['selection']).get(update.status) if update else False
                preview = self.new({'project_id': project.id, 'date_from': dashboard.date_from, 'date_to': dashboard.date_to})
                rows.append((project.display_name, project.company_id.display_name, status or _('Unavailable'),
                             '%s%%' % update.progress if update else _('Unavailable'),
                             dashboard._acme_monitoring_amount(preview.budget_variance, project.acme_contract_currency_id)
                             if preview.finance_available and preview.budget else _('Unavailable')))
            dashboard.acme_portfolio_html = table(
                [_('Project'), _('Company'), _('Status'), _('Reported Progress'), _('Budget Less Actual')], rows,
            )

    def action_acme_expenses(self):
        self.ensure_one()
        project = self.project_id
        project.check_access('read')
        if not project.account_id:
            raise UserError(_('Set the project analytic account to open project expenses.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('R-11 Project Analytic Expenses'),
            'res_model': 'account.analytic.line', 'view_mode': 'list,form',
            'domain': [(project.account_id.plan_id._column_name(), '=', project.account_id.id),
                       ('company_id', '=', project.company_id.id),
                       '|', ('move_line_id', '=', False), ('move_line_id.move_id.state', '=', 'posted'),
                       '|', ('general_account_id.account_type', 'in', ['expense', 'expense_direct_cost', 'expense_depreciation']),
                       '&', ('general_account_id', '=', False), ('amount', '<', 0)],
        }

    def action_acme_staff_advances(self):
        self.ensure_one()
        self.project_id.check_access('read')
        return {
            'type': 'ir.actions.act_window', 'name': _('Mobilization Work Advance Authorizations'),
            'res_model': 'approval.request', 'view_mode': 'list,form',
            'domain': [('acme_project_id', '=', self.project_id.id), ('acme_kind', 'in', ['work_advance', 'petty_cash']),
                       ('request_status', '=', 'approved')],
        }
