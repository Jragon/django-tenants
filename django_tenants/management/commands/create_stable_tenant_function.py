
from django.core.management import BaseCommand
from django.db import connection

from django_tenants.utils import create_stable_tenant_function


class Command(BaseCommand):

    help = 'Command creates stable function with a very high cost which returns tenant from the current settings.'

    def handle(self, *args, **options):
        create_stable_tenant_function()