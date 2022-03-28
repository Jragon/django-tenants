from django.conf import settings
from django.contrib.sites.shortcuts import get_current_site
from django.core.management import call_command
from django.db import connection, models, connections, transaction
from django.db.models.fields.related import RelatedField
from django.urls import reverse
from django.core import checks

from .postgresql_backend.base import _check_schema_name
from .utils import get_tenant_model, get_tenant_domain_model, get_public_schema_name, get_tenant_database_alias
from .fields import RLSForeignKey, generate_rls_fk_field


def get_tenant():
    tenant = connection.tenant
    if tenant is None:
        raise Exception(
            "No tenant configured in db connection, connection.tenant is none"
        )
    model = get_tenant_model()
    return (
        tenant if isinstance(tenant, model) else model(schema_name=tenant.schema_name)
    )

class TenantMixin(models.Model):
    """
    All tenant models must inherit this class.
    """

    schema_name = models.CharField(max_length=63, unique=True, db_index=True,
                                   validators=[_check_schema_name])

    domain_url = None
    """
    Leave this as None. Stores the current domain url so it can be used in the logs
    """
    domain_subfolder = None
    """
    Leave this as None. Stores the subfolder in subfolder routing was used
    """

    _previous_tenant = []

    class Meta:
        abstract = True

    def __str__(self):
        return self.schema_name

    def __enter__(self):
        """
        Syntax sugar which helps in celery tasks, cron jobs, and other scripts

        Usage:
            with Tenant.objects.get(schema_name='test') as tenant:
                # run some code in tenant test
            # run some code in previous tenant (public probably)
        """
        connection = connections[get_tenant_database_alias()]
        self._previous_tenant.append(connection.tenant)
        self.activate()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        connection = connections[get_tenant_database_alias()]

        connection.set_tenant(self._previous_tenant.pop())

    def activate(self):
        """
        Syntax sugar that helps at django shell with fast tenant changing

        Usage:
            Tenant.objects.get(schema_name='test').activate()
        """
        connection = connections[get_tenant_database_alias()]
        connection.set_tenant(self)

    @classmethod
    def deactivate(cls):
        """
        Syntax sugar, return to public schema

        Usage:
            test_tenant.deactivate()
            # or simpler
            Tenant.deactivate()
        """
        connection = connections[get_tenant_database_alias()]
        connection.set_schema_to_public()

    def save(self, verbosity=1, *args, **kwargs):
        connection = connections[get_tenant_database_alias()]
        is_new = self._state.adding
        has_schema = hasattr(connection, 'schema_name')
        if has_schema and is_new and connection.schema_name != get_public_schema_name():
            raise Exception("Can't create tenant outside the public schema. "
                            "Current schema is %s." % connection.schema_name)
        elif has_schema and not is_new and connection.schema_name not in (self.schema_name, get_public_schema_name()):
            raise Exception("Can't update tenant outside it's own schema or "
                            "the public schema. Current schema is %s."
                            % connection.schema_name)

        super().save(*args, **kwargs)

    def serializable_fields(self):
        """ in certain cases the user model isn't serializable so you may want to only send the id """
        return self

    def get_primary_domain(self):
        """
        Returns the primary domain of the tenant
        """
        try:
            domain = self.domains.get(is_primary=True)
            return domain
        except get_tenant_domain_model().DoesNotExist:
            return None

    def reverse(self, request, view_name):
        """
        Returns the URL of this tenant.
        """
        http_type = 'https://' if request.is_secure() else 'http://'

        domain = get_current_site(request).domain

        url = ''.join((http_type, self.schema_name, '.', domain, reverse(view_name)))

        return url

    def get_tenant_type(self):
        """
        Get the type of tenant. Will only work for multi type tenants
        :return: str
        """
        return getattr(self, settings.MULTI_TYPE_DATABASE_FIELD)



class MultitenantMixin(models.Model):
    """
    Mixin for any shared schema table (multitenant table). Adds a FK to the Tenant Model
    and enforces all constraints to the table to work with Row Level Security.
    """

    tenant = generate_rls_fk_field()

    class Meta:
        abstract = True

    @classmethod
    def check(cls, **kwargs):
        errors = super().check(**kwargs)
        errors.extend(cls._run_check_tenant_field())
        errors.extend(cls._run_check_m2m_fields())
        errors.extend(cls._run_check_unique_together())
        errors.extend(cls._run_check_uniques())
        return errors

    @classmethod
    def _get_tenant_field(cls):
        all_fields = cls._meta.get_fields()
        tenant_fields = [field for field in all_fields if field.name == "tenant"]
        tenant_field = tenant_fields[0] if tenant_fields else None
        return tenant_field

    @classmethod
    def _run_check_tenant_field(cls):
        tenant_field = cls._get_tenant_field()
        object_name = cls._meta.object_name

        # Ensure that tenant field are still present.
        if not tenant_field:
            return [
                checks.Critical(
                    f"tenant field not present in {object_name}",
                    obj=cls,
                    id=f"tenant_schemas.{object_name}.tenant_field.C001",
                )
            ]
        # Ensure that tenant field is instance of RLSForeignKey.
        elif not isinstance(tenant_field, RLSForeignKey):
            return [
                checks.WARNING(
                    f"tenant field isn't instance of {RLSForeignKey.__name__} in {object_name}",
                    obj=cls,
                    id=f"tenant_schemas.{object_name}.tenant_field.W001",
                )
            ]

        return list()

    @classmethod
    def _run_check_m2m_fields(cls):
        all_fields = cls._meta.get_fields()

        warnings = list()

        # Ensure that m2m field related model has tenant field and is an instance of RLSForeignKey
        m2m_fields = (
            field for field in all_fields if isinstance(field, models.ManyToManyField)
        )
        for m2m_field in m2m_fields:
            through_all_fields = m2m_field.remote_field.through._meta.get_fields()
            through_tenant_fields = [
                field for field in through_all_fields if field.name == "tenant"
            ]
            through_tenant_field = (
                through_tenant_fields[0] if through_tenant_fields else None
            )
            through_object_name = m2m_field.remote_field.through._meta.object_name

            auto_or_manual_model = (
                "auto-created"
                if m2m_field.remote_field.through._meta.auto_created
                else "manual"
            )

            if not through_tenant_field:
                warnings.append(
                    checks.Warning(
                        f"tenant field not present in Many2Many {auto_or_manual_model} model: {through_object_name}",
                        hint=f"Use custom defined model for through property in Many2Many field "
                        f"{cls._meta.object_name}.{m2m_field.name} using {MultitenantMixin.__name__} "
                        f"in the model definition",
                        id=f"tenant_schemas.{through_object_name}.m2m_field.W001",
                    )
                )
            elif not isinstance(through_tenant_field, RLSForeignKey):
                warnings.append(
                    checks.Warning(
                        f"tenant field isn't instance of RLSForeignKey in {through_object_name}",
                        id=f"tenant_schemas.{through_object_name}.m2m_field.W002",
                    )
                )

        return warnings

    @classmethod
    def _run_check_unique_together(cls):
        warnings = list()

        if cls._meta.unique_together:
            object_name = cls._meta.object_name
            tenant_field = cls._get_tenant_field()
            for unique_together in cls._meta.unique_together:
                if tenant_field.name not in unique_together:
                    warnings.append(
                        checks.Warning(
                            f"tenant field isn't in unique_together in {object_name}: {unique_together}",
                            id=f"tenant_schemas.{object_name}.unique_together_without_tenant.W001",
                        )
                    )

        return warnings

    @classmethod
    def _run_check_uniques(cls):
        warnings = list()

        for field in cls._meta.get_fields():
            object_name = cls._meta.object_name
            if (
                # related fields can be unique (ie 1-1 field is unique on pkeys so no worries)
                not isinstance(field, RelatedField)
                # pkeys are unique anyway
                and not getattr(field, "primary_key", False)
                and getattr(field, "unique", False)
            ):
                warnings.append(
                    checks.Warning(
                        f"Field {field.name} marked as unique in {object_name}. Must use unique together with the tenant_id.",
                        id=f"tenant_schemas.{object_name}.unique.W001",
                    )
                )

        return warnings


class DomainMixin(models.Model):
    """
    All models that store the domains must inherit this class
    """
    domain = models.CharField(max_length=253, unique=True, db_index=True)
    tenant = models.ForeignKey(settings.TENANT_MODEL, db_index=True, related_name='domains',
                               on_delete=models.CASCADE)

    # Set this to true if this is the primary domain
    is_primary = models.BooleanField(default=True, db_index=True)

    @transaction.atomic
    def save(self, *args, **kwargs):
        # Get all other primary domains with the same tenant
        domain_list = self.__class__.objects.filter(tenant=self.tenant, is_primary=True).exclude(pk=self.pk)
        # If we have no primary domain yet, set as primary domain by default
        self.is_primary = self.is_primary or (not domain_list.exists())
        if self.is_primary:
            # Remove primary status of existing domains for tenant
            domain_list.update(is_primary=False)
        super().save(*args, **kwargs)

    class Meta:
        abstract = True

    def __str__(self):
        return self.domain
