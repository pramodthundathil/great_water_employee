from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from organization.models import Organization, Branch, Department
from accounts.models import User, RoleChoices, UserBranchScope, EmployeeProfile


class AuthenticationAndPermissionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name="Great Waters", code="GW")
        self.branch_dxb = Branch.objects.create(organization=self.org, code="GW-DXB", name="Dubai Branch")
        self.branch_kch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi Branch")

        # Global Director
        self.director = User.objects.create_user(
            username="director",
            employee_id="GW-DIR-001",
            email="director@greatwaters.com",
            password="Password@123",
            role=RoleChoices.DIRECTOR,
            is_global_scope=True,
            primary_branch=self.branch_dxb
        )

        # Branch-scoped Admin Manager (Kochi only)
        self.admin_kch = User.objects.create_user(
            username="admin.kch",
            employee_id="GW-ADM-002",
            email="admin.kch@greatwaters.com",
            password="Password@123",
            role=RoleChoices.ADMIN_MANAGER,
            is_global_scope=False,
            primary_branch=self.branch_kch
        )

        # Employee (Dubai)
        self.emp_dxb = User.objects.create_user(
            username="emp.dxb",
            employee_id="GW-EMP-002",
            email="emp.dxb@greatwaters.com",
            password="Password@123",
            role=RoleChoices.EMPLOYEE,
            is_global_scope=False,
            primary_branch=self.branch_dxb
        )

    def test_jwt_login_with_employee_id(self):
        url = reverse("api_token_obtain_pair")
        response = self.client.post(url, {
            "username": "GW-DIR-001",
            "password": "Password@123"
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["employee_id"], "GW-DIR-001")

    def test_jwt_login_with_email(self):
        url = reverse("api_token_obtain_pair")
        response = self.client.post(url, {
            "username": "director@greatwaters.com",
            "password": "Password@123"
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)

    def test_jwt_login_invalid_credentials(self):
        url = reverse("api_token_obtain_pair")
        response = self.client.post(url, {
            "username": "GW-DIR-001",
            "password": "WrongPassword"
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_jwt_token_refresh_and_blacklist(self):
        # 1. Login to get refresh token
        login_resp = self.client.post(reverse("api_token_obtain_pair"), {
            "username": "GW-DIR-001",
            "password": "Password@123"
        })
        refresh_token = login_resp.data["refresh"]

        # 2. Refresh token
        refresh_resp = self.client.post(reverse("api_token_refresh"), {
            "refresh": refresh_token
        })
        self.assertEqual(refresh_resp.status_code, status.HTTP_200_OK)
        new_refresh = refresh_resp.data.get("refresh", refresh_token)

        # 3. Blacklist refresh token (Logout)
        logout_resp = self.client.post(reverse("api_token_blacklist"), {
            "refresh": new_refresh
        })
        self.assertEqual(logout_resp.status_code, status.HTTP_200_OK)

        # 4. Attempt to use blacklisted token should fail
        fail_resp = self.client.post(reverse("api_token_refresh"), {
            "refresh": new_refresh
        })
        self.assertEqual(fail_resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_branch_scoping_isolation(self):
        # Admin Kochi should only access Kochi branch
        kch_branches = self.admin_kch.get_accessible_branches()
        self.assertEqual(kch_branches.count(), 1)
        self.assertEqual(kch_branches.first(), self.branch_kch)
        self.assertFalse(self.admin_kch.can_access_branch(self.branch_dxb))

        # Director Global should access both Dubai and Kochi
        director_branches = self.director.get_accessible_branches()
        self.assertEqual(director_branches.count(), 2)
        self.assertTrue(self.director.can_access_branch(self.branch_dxb))
        self.assertTrue(self.director.can_access_branch(self.branch_kch))
