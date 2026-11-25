from collections import Counter, defaultdict
from datetime import timedelta

from markupsafe import Markup

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Domain
from odoo.tools.misc import format_amount


def ratio(numerator, denominator):
    return 100 * numerator / denominator if numerator is not None and denominator else None


def difference(left, right):
    return left - right if left is not None and right is not None else None


class ProjectDashboard(models.TransientModel):
    _inherit = 'acme.project.dashboard'

    scope = fields.Selection([('single', 'Single Project'), ('portfolio', 'Project Rollup')],
                             required=True, default='single')
    project_id = fields.Many2one(compute='_compute_report_project', store=False, required=False)
    project_ids = fields.Many2many('project.project')
    assigned_only = fields.Boolean(compute='_compute_assigned_only')
    progress_panel = fields.Html(compute='_compute_metrics')
    schedule_panel = fields.Html(compute='_compute_metrics')
    revenue_panel = fields.Html(compute='_compute_metrics')
    wip_panel = fields.Html(compute='_compute_metrics')
    budget_panel = fields.Html(compute='_compute_metrics')
    margin_panel = fields.Html(compute='_compute_metrics')
    acme_r13_summary = fields.Html(compute='_compute_metrics')

    @api.depends_context('uid')
    def _compute_assigned_only(self):
        self.assigned_only = not self.env['project.project']._acme_has_financial_role()

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._acme_legacy_filter_values(vals) for vals in vals_list])

    def write(self, vals):
        return super().write(self._acme_legacy_filter_values(vals))

    def _acme_legacy_filter_values(self, vals):
        if 'project_id' not in vals:
            return vals
        vals = dict(vals)
        project_id = vals.pop('project_id')
        vals.setdefault('project_ids', [Command.set([project_id] if project_id else [])])
        return vals

    @api.depends('project_ids')
    def _compute_report_project(self):
        for dashboard in self:
            dashboard.project_id = dashboard.project_ids if len(dashboard.project_ids) == 1 else False

    @api.constrains('project_ids')
    def _check_selection(self):
        for dashboard in self:
            dashboard.project_ids._acme_check_dashboard_access()

    def _acme_selected_projects(self):
        self.ensure_one()
        if self.project_ids:
            self.project_ids._acme_check_dashboard_access()
            return self.project_ids
        projects = self.env['project.project']
        domain = [('company_id', 'in', self.env.companies.ids), ('is_template', '=', False)]
        if not projects._acme_has_financial_role():
            domain.append(('user_id', '=', self.env.uid))
        return projects.search(domain)

    def _acme_overdue_domain(self, projects):
        planned = Domain('acme_end_date', '>=', self.date_from) & Domain('acme_end_date', '<=', self.date_to)
        deadline = (Domain('acme_end_date', '=', False) & Domain('date_deadline', '>=', self.date_from)
                    & Domain('date_deadline', '<', self.date_to + timedelta(days=1)))
        return (Domain('project_id', 'in', projects.ids) & Domain('acme_open_overdue', '=', True)
                & (planned | deadline))

    def action_dashboard_overdue(self):
        projects = self._acme_selected_projects()
        if not projects:
            raise ValidationError(_('There are no accessible projects to review.'))
        action = projects[0].action_acme_overdue_tasks()
        action['domain'] = list(self._acme_overdue_domain(projects))
        return action

    def _acme_task_metrics(self, projects):
        tasks = self.env['project.task'].search([
            ('project_id', 'in', projects.ids), ('state', '!=', '1_canceled'), ('is_template', '=', False),
        ])
        # A parent with children is a work package, even when a child is cancelled.
        leaves = tasks.filtered(lambda task: not task.child_ids)
        scheduled = leaves.filtered(lambda task: (due := task._acme_delay_due_date())
                                    and self.date_from and self.date_to and self.date_from <= due <= self.date_to)
        return Counter(
            task.project_id.id for task in leaves
        ), Counter(task.project_id.id for task in leaves if task.state == '1_done'), len(scheduled), len(
            scheduled.filtered(lambda task: task.state == '1_done')
        )

    def _acme_panel_data(self):
        self.ensure_one()
        projects = self._acme_selected_projects()
        counts, done, scheduled, scheduled_done = self._acme_task_metrics(projects)
        today = fields.Date.context_today(self)
        buckets = defaultdict(list)
        notes = set()
        for project in projects:
            currency = project.acme_contract_currency_id
            if not currency:
                notes.add(_('%s has no contract currency.', project.display_name))
                continue
            values = project._acme_dashboard_financials(today, self.date_from, self.date_to,
                                                       done[project.id], counts[project.id])
            notes.update(values.pop('issues'))
            buckets[currency].append(values)
        totals = {}
        for currency, entries in buckets.items():
            totals[currency] = {
                key: sum(entry[key] for entry in entries) if all(entry[key] is not None for entry in entries) else None
                for key in entries[0] if key != 'costs'
            }
            totals[currency]['costs'] = {
                key: sum(entry['costs'].get(key, 0) for entry in entries)
                for key in sorted(set().union(*(entry['costs'] for entry in entries)))
            }
        delays = Counter()
        if projects and self.date_from and self.date_to:
            labels = dict(self.env['project.task']._fields['acme_delay_reason']._description_selection(self.env))
            for task in self.env['project.task'].search(self._acme_overdue_domain(projects)):
                delays[labels.get(task.acme_delay_reason) or _('No reason recorded')] += 1
        return totals, sum(counts.values()), sum(done.values()), scheduled, scheduled_done, delays, sorted(notes)

    @api.depends('project_ids', 'date_from', 'date_to')
    @api.depends_context('uid', 'company')
    def _compute_metrics(self):
        def percent(value):
            return _('Unavailable') if value is None else '%.2f%%' % value

        def headline(label, value):
            return Markup('<div class="acme_metric_label">%s</div><div class="acme_kpi">%s</div>') % (label, value)

        def details(items):
            return Markup('<dl class="acme_metric_details">%s</dl>') % Markup('').join(
                Markup('<div><dt>%s</dt><dd>%s</dd></div>') % (label, value) for label, value in items
            )

        for dashboard in self:
            totals, tasks, done, scheduled, scheduled_done, _delays, notes = dashboard._acme_panel_data()
            dashboard.task_count, dashboard.done_count = tasks, done
            dashboard.scheduled_count, dashboard.scheduled_done = scheduled, scheduled_done
            completion = ratio(done, tasks)
            schedule = ratio(scheduled_done, scheduled)
            dashboard.progress = completion or 0
            dashboard.schedule_percent = schedule or 0
            physical = [
                (_('Physical BOQ Progress (%s)', currency.name), percent(ratio(data['physical'], data['boq'])))
                for currency, data in totals.items() if ratio(data['physical'], data['boq']) is not None
            ]
            dashboard.progress_panel = headline(_('Task Completion'), percent(completion)) + (
                details(physical) if physical else Markup('')
            )
            dashboard.schedule_panel = headline(_('Schedule Performance'), percent(schedule)) + details([
                (_('Planned'), scheduled), (_('Actual (Done)'), scheduled_done),
            ])
            panels = defaultdict(list)
            for currency, data in totals.items():
                def money(value):
                    return _('Unavailable') if value is None else format_amount(self.env, value, currency)

                contract, billed, earned, actual = (data[key] for key in ('contract', 'billed', 'earned', 'actual'))
                contract_margin = difference(contract, actual)
                earned_margin = difference(earned, actual)
                wip = difference(earned, billed)
                wip_percent = ratio(wip, contract)
                wip_details = [(_('Period Incurred Cost'), money(data['period_cost']))]
                if wip_percent is not None:
                    wip_details.insert(0, (_('Financial WIP'), money(wip)))
                sections = {
                    'revenue_panel': details([
                        (_('Advance Paid'), money(data['advance_paid'])),
                        (_('Advance Recovered'), money(data['advance_recovered'])),
                        (_('Advance Outstanding'), money(data['advance_outstanding'])),
                        (_('Certified Retention'), money(data['retention'])),
                        (_('Approved Operations Budget'), money(data['operations_budget'])),
                        (_('Earned Margin to Date'), money(earned_margin)),
                        (_('Earned Margin %'), percent(ratio(earned_margin, earned) if earned and earned > 0 else None)),
                        (_('Invoiced Margin'), money(difference(billed, actual))),
                    ]),
                    'wip_panel': headline(_('WIP % of Contract'), percent(wip_percent)) + details(wip_details),
                    'budget_panel': headline(_('Burn %'), percent(ratio(actual, data['budget']))) + details([
                        (_('Internal BOQ Cost Build-up'), money(data['budget'])),
                        (_('Actual Cost'), money(actual)),
                        (_('Variance'), money(difference(data['budget'], actual))),
                    ]),
                    'margin_panel': headline(_('Contract Profitability'), percent(ratio(contract_margin, contract))) + details([
                        (_('Contract Value (untaxed)'), money(contract)),
                        (_('Earned Revenue'), money(earned)),
                        (_('Invoiced Progress Revenue'), money(billed)),
                        (_('Actual Cost'), money(actual)),
                        (_('Contract Value Less Cost to Date'), money(contract_margin)),
                    ]),
                }
                for name, content in sections.items():
                    panels[name].append(Markup('<h3 class="acme_card_currency">%s</h3>%s') % (currency.name, content))
            for name in ('revenue_panel', 'wip_panel', 'budget_panel', 'margin_panel'):
                dashboard[name] = Markup('').join(panels[name]) or _('Unavailable')
            projects = dashboard._acme_selected_projects()
            updates = self.env['project.update'].search([('project_id', 'in', projects.ids)], order='date desc,id desc')
            latest = {}
            for update in updates:
                latest.setdefault(update.project_id.id, update.status)
            labels = dict(updates._fields['status']._description_selection(self.env))
            statuses = [(project.display_name, labels.get(latest.get(project.id)) or _('Unavailable'))
                        for project in projects]
            dashboard.acme_r13_summary = details(statuses) if statuses else _('No accessible projects.')
            single = next(iter(totals.values())) if len(totals) == 1 else {}
            dashboard.revenue = single.get('contract') or 0
            dashboard.budget = single.get('budget') or 0
            dashboard.actual_cost = single.get('actual') or 0
            dashboard.wip_cost = single.get('period_cost') or 0
            dashboard.margin_amount = difference(single.get('contract'), single.get('actual')) or 0
            dashboard.profitability_percent = ratio(dashboard.margin_amount, single.get('contract')) or 0
            dashboard.budget_variance = difference(single.get('budget'), single.get('actual')) or 0
            dashboard.finance_available = bool(single and single['actual'] is not None)
            dashboard.finance_note = ' '.join(notes)

    def action_refresh(self):
        self.ensure_one()
        self._acme_selected_projects()
        self.invalidate_recordset()
        return {'type': 'ir.actions.act_window', 'name': _('Project Financial & Progress Dashboard'),
                'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'current'}


class ProjectDashboardAction(models.Model):
    _inherit = 'project.project'

    def _acme_check_dashboard_access(self):
        if not self:
            return
        self.check_access('read')
        portfolio_access = self._acme_has_financial_role()
        if any(project.company_id not in self.env.companies or (
            not portfolio_access and project.user_id != self.env.user
        ) for project in self):
            raise AccessError(_('You can review only dashboard projects assigned to you in your selected companies.'))

    def _acme_dashboard_financials(self, today, date_from, date_to, done, tasks):
        self.ensure_one()
        from .costing import COST_CATEGORIES

        issues = []
        try:
            costing = self._acme_costing_snapshot(today, date_from, period_date_to=date_to)
        except AccessError:
            costing = {}
            issues.append(_('Accounting or analytic records are unavailable for %s.', self.display_name))
        issues.extend(costing.get('issues', []))
        contract = costing.get('contract')
        billed = costing.get('billed') if not costing.get('commercial_incomplete') else None
        pending = costing.get('pending')
        if contract is None:
            try:
                contract, billed, pending, eligible, legacy = self._acme_commercial_snapshot(today)
                if legacy:
                    billed = None
            except AccessError:
                pass
        actual = costing.get('actual') if not costing.get('cost_issues') else None
        period_cost = costing.get('period_cost') if actual is not None else None
        try:
            certificates = self.env['acme.project.certificate'].search([
                ('project_id', '=', self.id), ('acme_certificate_type', '=', 'progress'),
                ('state', '=', 'certified'), ('date', '<=', today),
            ])
            retention = sum(certificates.mapped('retention_amount'))
            boq_lines = self.acme_boq_ids
            boq = sum(line.quantity * line.selling_rate for line in boq_lines) if boq_lines else None
            physical = None
            verified = certificates.filtered(lambda cert: cert.acme_ipc_managed and cert.acme_verified_evidence)
            legacy = (certificates - verified).filtered(lambda cert: not cert.acme_ipc_managed
                                                       and cert.signed_takeoff and cert.acme_pc_snapshot)
            measurements = (verified | legacy).line_ids.filtered(
                lambda line: line.boq_id in boq_lines and line.quantity > 0
            )
            if measurements:
                # Accepted delivery quantities can recur on the supporting document. Use the
                # authorized allocation to the round, not the delivery document's cumulative total.
                physical = sum(
                    (line.acme_site_quantity if line.certificate_id in verified
                     and line.acme_billing_basis == 'measured' else line.quantity) * line.boq_id.selling_rate
                    for line in measurements
                )
        except AccessError:
            boq = physical = retention = None
            issues.append(_('Signed BOQ or certificate evidence is unavailable for %s.', self.display_name))
        basis = self.company_id.acme_dashboard_progress_basis
        progress = ratio(done, tasks) if basis == 'tasks' else ratio(physical, boq) if basis == 'boq' else None
        if not basis:
            issues.append(_('Finance has not approved a dashboard earned revenue basis for %s.', self.company_id.name))
        earned = contract * progress / 100 if contract and contract > 0 and progress is not None else None
        labels = dict(COST_CATEGORIES)
        return {
            'contract': contract, 'billed': billed, 'earned': earned, 'pending': pending,
            'actual': actual, 'period_cost': period_cost,
            'budget': self.acme_cost_budget or None, 'operations_budget': costing.get('budget') or None,
            'boq': boq, 'physical': physical, 'advance_paid': self.acme_advance_paid,
            'advance_recovered': self.acme_recovered, 'advance_outstanding': self.acme_advance_balance,
            'retention': retention, 'costs': {_(labels.get(key, key)): value
                                            for key, value in costing.get('costs', {}).items()},
            'issues': issues,
        }

    def action_acme_dashboard(self):
        self.ensure_one()
        self._acme_check_dashboard_access()
        return {'type': 'ir.actions.act_window', 'name': _('Project Financial & Progress Dashboard'),
                'res_model': 'acme.project.dashboard', 'view_mode': 'form',
                'context': {'default_project_ids': [Command.set(self.ids)]}}

class CompanyDashboardProgress(models.Model):
    _inherit = 'res.company'

    acme_dashboard_progress_basis = fields.Selection(
        [('tasks', 'Task Completion'), ('boq', 'Weighted Physical BOQ Progress')],
        string='Dashboard Earned Revenue Progress Basis',
        help='Finance reporting basis for the dashboard. This does not change the WIP journal entry policy.',
    )

    @api.constrains('acme_dashboard_progress_basis')
    def _check_dashboard_progress_basis(self):
        if not self.env.su and not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_('Only Finance managers can change the dashboard earned revenue basis.'))

