from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError


class ProjectDashboard(models.TransientModel):
    _name = 'acme.project.dashboard'
    _description = 'Project Performance Dashboard'
    _check_company_auto = True

    project_id = fields.Many2one('project.project', required=True, check_company=True)
    company_id = fields.Many2one(related='project_id.company_id')
    currency_id = fields.Many2one(related='project_id.acme_contract_currency_id')
    date_from = fields.Date(default=lambda self: fields.Date.context_today(self).replace(day=1), required=True)
    date_to = fields.Date(default=fields.Date.context_today, required=True)
    task_count = fields.Integer(compute='_compute_metrics')
    done_count = fields.Integer(compute='_compute_metrics')
    progress = fields.Float(compute='_compute_metrics')
    scheduled_count = fields.Integer(compute='_compute_metrics')
    scheduled_done = fields.Integer(compute='_compute_metrics')
    schedule_percent = fields.Float(compute='_compute_metrics')
    revenue = fields.Monetary(compute='_compute_metrics', string='Contract Value Excluding Tax')
    budget = fields.Monetary(compute='_compute_metrics', string='Tender Cost Build-up Budget')
    actual_cost = fields.Monetary(compute='_compute_metrics', string='Lifetime Analytic Cost')
    wip_cost = fields.Monetary(compute='_compute_metrics', string='Period Cost / Operational WIP')
    margin_amount = fields.Monetary(compute='_compute_metrics', string='Contract Value Less Incurred Cost')
    profitability_percent = fields.Float(compute='_compute_metrics')
    budget_variance = fields.Monetary(compute='_compute_metrics', string='Budget Less Actual Cost')
    finance_available = fields.Boolean(compute='_compute_metrics')
    finance_note = fields.Char(compute='_compute_metrics')

    customer_id = fields.Many2one(related='project_id.partner_id', string='Customer')
    project_manager_id = fields.Many2one(related='project_id.user_id', string='Project Manager')

    @api.depends('project_id.display_name')
    def _compute_display_name(self):
        for dashboard in self:
            dashboard.display_name = _('%s — Project Dashboard', dashboard.project_id.display_name) if dashboard.project_id else _('Project Dashboard')

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for dashboard in self:
            if dashboard.date_from > dashboard.date_to:
                raise ValidationError(_('Dashboard dates must be in chronological order.'))

    @api.depends('project_id', 'date_from', 'date_to')
    def _compute_metrics(self):
        for dashboard in self:
            project = dashboard.project_id
            dashboard.task_count = dashboard.done_count = dashboard.scheduled_count = dashboard.scheduled_done = 0
            dashboard.progress = dashboard.schedule_percent = dashboard.profitability_percent = 0
            dashboard.actual_cost = dashboard.wip_cost = dashboard.margin_amount = dashboard.budget_variance = 0
            dashboard.finance_available = False
            dashboard.finance_note = False
            dashboard.revenue = dashboard.budget = 0
            if not project:
                continue
            project.check_access('read')
            tasks = self.env['project.task'].search([('project_id', '=', project.id), ('state', '!=', '1_canceled'), ('is_template', '=', False)])
            # Count work packages once: a parent with visible children is not another work unit.
            parent_ids = set(tasks.mapped('parent_id').ids)
            leaves = tasks.filtered(lambda task: task.id not in parent_ids)
            dashboard.task_count = len(leaves)
            dashboard.done_count = len(leaves.filtered(lambda task: task.state == '1_done'))
            dashboard.progress = 100 * dashboard.done_count / dashboard.task_count if dashboard.task_count else 0
            scheduled = leaves.filtered(lambda task: task.acme_end_date and dashboard.date_from <= task.acme_end_date <= dashboard.date_to)
            dashboard.scheduled_count = len(scheduled)
            dashboard.scheduled_done = len(scheduled.filtered(lambda task: task.state == '1_done'))
            dashboard.schedule_percent = 100 * dashboard.scheduled_done / dashboard.scheduled_count if dashboard.scheduled_count else 0
            if not project.account_id:
                dashboard.finance_note = _('Set the existing project analytic account to show cost metrics.')
                continue
            if not project.acme_contract_currency_id:
                dashboard.finance_note = _('A contract currency is required for the financial dashboard.')
                continue
            try:
                costing = project._acme_costing_snapshot(dashboard.date_to, dashboard.date_from)
                dashboard.revenue = costing['contract']
                dashboard.budget = costing['budget'] or 0
                dashboard.actual_cost = costing['actual']
                dashboard.wip_cost = costing['period_cost']
                dashboard.margin_amount = costing['billed'] - dashboard.actual_cost
                dashboard.profitability_percent = 100 * dashboard.margin_amount / costing['billed'] if costing['billed'] else 0
                dashboard.budget_variance = dashboard.budget - dashboard.actual_cost if dashboard.budget else 0
                dashboard.finance_available = not costing['issues']
                dashboard.finance_note = (' '.join(costing['issues']) if costing['issues'] else
                                          _('Project actuals use the analytic ledger. Invoiced margin excludes VAT and customer advances.'))
            except AccessError:
                dashboard.finance_note = _('Your accounting/analytic access does not permit these cost metrics. No unrestricted data is substituted.')

    def action_refresh(self):
        self.ensure_one()
        self.invalidate_recordset()
        return {'type': 'ir.actions.act_window', 'name': _('Project Dashboard'), 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'current'}


class ProjectDashboardAction(models.Model):
    _inherit = 'project.project'

    def action_acme_dashboard(self):
        self.ensure_one()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('Project Dashboard'), 'res_model': 'acme.project.dashboard', 'view_mode': 'form', 'context': {'default_project_id': self.id}}
