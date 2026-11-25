from datetime import timedelta

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError


_WIP_TOKEN = object()
_WIP_FIELDS = {'acme_wip_project_id', 'acme_wip_cutoff', 'acme_wip_target',
               'acme_wip_posted_balance', 'acme_wip_reversal'}


class DashboardWip(models.TransientModel):
    _inherit = 'acme.project.dashboard'

    def action_acme_period_wip(self):
        self.ensure_one()
        self.check_access('read')
        if not self.env.user.has_group('account.group_account_user'):
            raise AccessError(_('Only Finance can prepare a project WIP adjustment.'))
        project, cutoff = self.project_id, self.date_to
        project.check_access('read')
        if not project or not cutoff or cutoff > fields.Date.context_today(self):
            raise UserError(_('Select a project and an elapsed period cutoff.'))
        company = project.company_id
        if company not in self.env.companies:
            raise AccessError(_('Select the project company before preparing WIP.'))
        self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [project.id])
        self.env.invalidate_all()
        Move = self.env['account.move'].with_company(company)
        entry = Move.search([('acme_wip_project_id', '=', project.id), ('acme_wip_cutoff', '=', cutoff)], limit=1)
        if not entry:
            entry = self._acme_prepare_period_wip(project, cutoff, Move)
        if not entry:
            return {'type': 'ir.actions.client', 'tag': 'display_notification',
                    'params': {'type': 'success', 'message': _('The posted WIP balance already matches the target. No adjustment is due.')}}
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'view_mode': 'form', 'res_id': entry.id}

    def _acme_prepare_period_wip(self, project, cutoff, Move):
        company = project.company_id
        journal = company.acme_wip_journal_id
        asset = company.acme_wip_asset_account_id
        liability = company.acme_wip_liability_account_id
        offset = company.acme_wip_offset_account_id
        policy = company.acme_wip_reversal_policy
        accounts = asset | liability | offset
        if (not journal or not journal.active or journal.company_id != company or journal.type != 'general'
                or len(accounts) != 3 or any(not account.active or company not in account.company_ids for account in accounts)
                or asset.account_type != 'asset_current' or liability.account_type != 'liability_current'
                or offset.account_type not in ('income', 'income_other') or not policy):
            raise UserError(_('Configure a general journal, distinct active WIP asset, liability and revenue accounts, and a reversal policy in this company.'))
        data = project._acme_costing_snapshot(cutoff)
        if data.get('wip') is None or data.get('issues'):
            raise UserError(_('WIP requires an approved revenue method and complete source data: %s',
                              ' '.join(data.get('issues', [])) or _('the earned or invoiced figures are unavailable')))
        currency = company.currency_id
        target = project.acme_contract_currency_id._convert(data['wip'], currency, company, cutoff)
        prior = Move.search([('acme_wip_project_id', '=', project.id), ('state', '=', 'posted'), ('date', '<=', cutoff)])
        historical_accounts = prior.line_ids.account_id.filtered(
            lambda account: account.account_type in ('asset_current', 'liability_current')
        )
        posted = self._acme_posted_wip_by_account(project, cutoff, asset | liability | historical_accounts)
        if any(not currency.is_zero(posted.get(account.id, 0)) for account in historical_accounts - (asset | liability)):
            raise UserError(_('Clear or migrate the remaining WIP balance on previously configured accounts before preparing a new adjustment.'))
        distribution = project._get_analytic_distribution()
        commands = []
        for account, balance in ((asset, max(target, 0)), (liability, min(target, 0))):
            delta = currency.round(balance - posted.get(account.id, 0))
            if currency.is_zero(delta):
                continue
            for destination, signed in ((account, delta), (offset, -delta)):
                commands.append(Command.create({
                    'name': _('Project WIP through %s', cutoff), 'account_id': destination.id,
                    'debit': max(signed, 0), 'credit': max(-signed, 0),
                    'analytic_distribution': distribution,
                }))
        if not commands:
            return Move
        return Move.with_context(acme_wip_token=_WIP_TOKEN).create({
            'move_type': 'entry', 'company_id': company.id, 'journal_id': journal.id, 'date': cutoff,
            'ref': _('Project WIP — %(project)s — %(cutoff)s', project=project.display_name, cutoff=cutoff),
            'acme_wip_project_id': project.id, 'acme_wip_cutoff': cutoff,
            'acme_wip_target': target, 'acme_wip_posted_balance': sum(posted.values()),
            'acme_wip_reversal': policy, 'line_ids': commands,
        })

    def _acme_posted_wip_by_account(self, project, cutoff, accounts):
        lines = self.env['account.move.line'].search([
            ('company_id', '=', project.company_id.id), ('parent_state', '=', 'posted'), ('date', '<=', cutoff),
            ('account_id', 'in', accounts.ids), ('analytic_distribution', 'in', project.account_id.ids),
        ])
        totals = dict.fromkeys(accounts.ids, 0)
        account_key = str(project.account_id.id)
        for line in lines:
            share = sum(percent for ids, percent in (line.analytic_distribution or {}).items()
                        if account_key in ids.split(','))
            totals[line.account_id.id] += line.balance * share / 100
        return totals


class ProjectWipMove(models.Model):
    _inherit = 'account.move'

    acme_wip_project_id = fields.Many2one('project.project', readonly=True, check_company=True, ondelete='restrict', copy=False)
    acme_wip_cutoff = fields.Date(readonly=True, copy=False)
    acme_wip_target = fields.Monetary(currency_field='company_currency_id', readonly=True, copy=False)
    acme_wip_posted_balance = fields.Monetary(currency_field='company_currency_id', readonly=True, copy=False)
    acme_wip_reversal = fields.Selection(
        [('manual', 'Finance Selects Reversal Date'), ('next_day', 'Reverse on First Day after Cutoff')],
        readonly=True, copy=False,
    )
    _acme_unique_period_wip = models.Constraint(
        'UNIQUE(acme_wip_project_id, acme_wip_cutoff)', 'This project already has a WIP entry for this cutoff.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('acme_wip_token') is not _WIP_TOKEN and (
            any(_WIP_FIELDS.intersection(vals) for vals in vals_list)
            or any(self.env.context.get(f'default_{field}') for field in _WIP_FIELDS)
        ):
            raise AccessError(_('Prepare WIP entries from the project dashboard.'))
        return super().create(vals_list)

    def write(self, vals):
        if _WIP_FIELDS.intersection(vals):
            raise AccessError(_('The original WIP basis and project link are retained for audit.'))
        return super().write(vals)

    def action_reverse(self):
        if len(self) == 1 and self.acme_wip_cutoff:
            existing = self.reversal_move_ids.filtered(lambda move: move.state != 'cancel')[:1]
            if existing:
                return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                        'view_mode': 'form', 'res_id': existing.id}
        action = super().action_reverse()
        if len(self) == 1 and self.acme_wip_cutoff and self.acme_wip_reversal == 'next_day':
            action['context'] = dict(action.get('context', {}), default_date=self.acme_wip_cutoff + timedelta(days=1))
        return action

    def _post(self, soft=True):
        wip = self.filtered('acme_wip_cutoff')
        if wip.filtered(lambda move: move.date != move.acme_wip_cutoff):
            raise UserError(_('The WIP journal date must match its original period cutoff.'))
        result = super()._post(soft=soft)
        if (wip & result).filtered(lambda move: move.date != move.acme_wip_cutoff):
            raise UserError(_('The original WIP cutoff is locked. Finance must review the period before posting.'))
        return result

    def _reverse_moves(self, default_values_list=None, cancel=False):
        defaults = default_values_list if default_values_list is not None else [{} for _move in self]
        if self.filtered('acme_wip_cutoff'):
            defaults = [dict(values) for values in defaults]
            for move, values in zip(self, defaults):
                if not move.acme_wip_cutoff:
                    continue
                self.env.cr.execute('UPDATE account_move SET id = id WHERE id = %s', [move.id])
                move.invalidate_recordset(['reversal_move_ids'])
                if move.reversal_move_ids.filtered(lambda reversal: reversal.state != 'cancel'):
                    raise UserError(_('Open the existing WIP reversal for this period.'))
                if move.acme_wip_reversal == 'next_day':
                    expected = move.acme_wip_cutoff + timedelta(days=1)
                    if values.get('date') and fields.Date.to_date(values['date']) != expected:
                        raise UserError(_('This WIP adjustment must reverse on the day after its cutoff.'))
                    values['date'] = expected
        return super()._reverse_moves(defaults, cancel=cancel)
