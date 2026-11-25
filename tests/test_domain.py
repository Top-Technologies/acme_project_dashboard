from odoo import Command
from odoo.tests.common import new_test_user, tagged
from odoo.exceptions import UserError
from odoo.addons.acme_project_core.tests.test_project_flow import ProjectFlowCase

@tagged('post_install', '-at_install')
class TestDashboard(ProjectFlowCase):

    def test_dashboard_leaf_counts_and_period(self):
        parent = self.env['project.task'].create({'name': 'WBS', 'project_id': self.project.id})
        self.env['project.task'].create([{'name': 'A', 'project_id': self.project.id, 'parent_id': parent.id, 'acme_end_date': '2026-11-20', 'state': '1_done'}, {'name': 'B', 'project_id': self.project.id, 'parent_id': parent.id, 'acme_end_date': '2026-12-20'}])
        dashboard = self.env['acme.project.dashboard'].create({'project_id': self.project.id, 'date_from': '2026-11-01', 'date_to': '2026-11-30'})
        self.assertEqual(dashboard.task_count, 2)
        self.assertEqual(dashboard.progress, 50)
        self.assertEqual(dashboard.scheduled_count, 1)
        self.assertEqual(dashboard.schedule_percent, 100)
        self.assertFalse(dashboard.finance_available)

    def test_date_changes_leave_overall_progress_unchanged(self):
        self.env['project.task'].create([
            {'name': 'Completed work', 'project_id': self.project.id, 'acme_end_date': '2026-11-20', 'state': '1_done'},
            {'name': 'Future work', 'project_id': self.project.id, 'acme_end_date': '2026-12-20'},
        ])
        dashboard = self.env['acme.project.dashboard'].create({'project_id': self.project.id, 'date_from': '2026-11-01', 'date_to': '2026-11-30'})
        self.assertEqual(dashboard.progress, 50)
        self.assertEqual(dashboard.scheduled_done, 1)
        dashboard.write({'date_from': '2026-12-01', 'date_to': '2026-12-31'})
        self.assertEqual(dashboard.progress, 50)
        self.assertEqual(dashboard.scheduled_count, 1)
        self.assertEqual(dashboard.scheduled_done, 0)

    def test_dashboard_title_and_project_summary(self):
        dashboard = self.env['acme.project.dashboard'].create({'project_id': self.project.id})
        self.assertIn(self.project.display_name, dashboard.display_name)
        self.assertNotIn('acme.project.dashboard,', dashboard.display_name)
        self.assertEqual(dashboard.customer_id, self.project.partner_id)
        self.assertEqual(dashboard.project_manager_id, self.project.user_id)
