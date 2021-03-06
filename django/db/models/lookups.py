from copy import copy
import inspect

from django.conf import settings
from django.utils import timezone
from django.utils.functional import cached_property


class RegisterLookupMixin(object):
    def get_lookup(self, lookup_name):
        try:
            return self.class_lookups[lookup_name]
        except KeyError:
            # To allow for inheritance, check parent class class lookups.
            for parent in inspect.getmro(self.__class__):
                if not 'class_lookups' in parent.__dict__:
                    continue
                if lookup_name in parent.class_lookups:
                    return parent.class_lookups[lookup_name]
        except AttributeError:
            # This class didn't have any class_lookups
            pass
        if hasattr(self, 'output_type'):
            return self.output_type.get_lookup(lookup_name)
        return None

    @classmethod
    def register_lookup(cls, lookup):
        if not 'class_lookups' in cls.__dict__:
            cls.class_lookups = {}
        cls.class_lookups[lookup.lookup_name] = lookup

    @classmethod
    def _unregister_lookup(cls, lookup):
        """
        Removes given lookup from cls lookups. Meant to be used in
        tests only.
        """
        del cls.class_lookups[lookup.lookup_name]


class Transform(RegisterLookupMixin):
    def __init__(self, lhs, lookups):
        self.lhs = lhs
        self.init_lookups = lookups[:]

    def as_sql(self, qn, connection):
        raise NotImplementedError

    @cached_property
    def output_type(self):
        return self.lhs.output_type

    def relabeled_clone(self, relabels):
        return self.__class__(self.lhs.relabeled_clone(relabels))

    def get_group_by_cols(self):
        return self.lhs.get_group_by_cols()


class Lookup(RegisterLookupMixin):
    lookup_name = None

    def __init__(self, lhs, rhs):
        self.lhs, self.rhs = lhs, rhs
        self.rhs = self.get_prep_lookup()

    def get_prep_lookup(self):
        return self.lhs.output_type.get_prep_lookup(self.lookup_name, self.rhs)

    def get_db_prep_lookup(self, value, connection):
        return (
            '%s', self.lhs.output_type.get_db_prep_lookup(
                self.lookup_name, value, connection, prepared=True))

    def process_lhs(self, qn, connection, lhs=None):
        lhs = lhs or self.lhs
        return qn.compile(lhs)

    def process_rhs(self, qn, connection, rhs=None):
        value = rhs or self.rhs
        # Due to historical reasons there are a couple of different
        # ways to produce sql here. get_compiler is likely a Query
        # instance, _as_sql QuerySet and as_sql just something with
        # as_sql. Finally the value can of course be just plain
        # Python value.
        if hasattr(value, 'get_compiler'):
            value = value.get_compiler(connection=connection)
        if hasattr(value, 'as_sql'):
            sql, params = qn.compile(value)
            return '(' + sql + ')', params
        if hasattr(value, '_as_sql'):
            sql, params = value._as_sql(connection=connection)
            return '(' + sql + ')', params
        else:
            return self.get_db_prep_lookup(value, connection)

    def relabeled_clone(self, relabels):
        new = copy(self)
        new.lhs = new.lhs.relabeled_clone(relabels)
        if hasattr(new.rhs, 'relabeled_clone'):
            new.rhs = new.rhs.relabeled_clone(relabels)
        return new

    def get_group_by_cols(self):
        cols = self.lhs.get_group_by_cols()
        if hasattr(self.rhs, 'get_group_by_cols'):
            cols.extend(self.rhs.get_group_by_cols())
        return cols

    def as_sql(self, qn, connection):
        raise NotImplementedError


class BuiltinLookup(Lookup):
    def as_sql(self, qn, connection):
        lhs_sql, params = self.process_lhs(qn, connection)
        field_internal_type = self.lhs.output_type.get_internal_type()
        db_type = self.lhs.output_type
        lhs_sql = connection.ops.field_cast_sql(db_type, field_internal_type) % lhs_sql
        lhs_sql = connection.ops.lookup_cast(self.lookup_name) % lhs_sql
        rhs_sql, rhs_params = self.process_rhs(qn, connection)
        params.extend(rhs_params)
        operator_plus_rhs = self.get_rhs_op(connection, rhs_sql)
        return '%s %s' % (lhs_sql, operator_plus_rhs), params

    def get_rhs_op(self, connection, rhs):
        return connection.operators[self.lookup_name] % rhs


default_lookups = {}


class Exact(BuiltinLookup):
    lookup_name = 'exact'
default_lookups['exact'] = Exact


class IExact(BuiltinLookup):
    lookup_name = 'iexact'
default_lookups['iexact'] = IExact


class Contains(BuiltinLookup):
    lookup_name = 'contains'
default_lookups['contains'] = Contains


class IContains(BuiltinLookup):
    lookup_name = 'icontains'
default_lookups['icontains'] = IContains


class GreaterThan(BuiltinLookup):
    lookup_name = 'gt'
default_lookups['gt'] = GreaterThan


class GreaterThanOrEqual(BuiltinLookup):
    lookup_name = 'gte'
default_lookups['gte'] = GreaterThanOrEqual


class LessThan(BuiltinLookup):
    lookup_name = 'lt'
default_lookups['lt'] = LessThan


class LessThanOrEqual(BuiltinLookup):
    lookup_name = 'lte'
default_lookups['lte'] = LessThanOrEqual


class In(BuiltinLookup):
    lookup_name = 'in'

    def get_db_prep_lookup(self, value, connection):
        params = self.lhs.output_type.get_db_prep_lookup(
            self.lookup_name, value, connection, prepared=True)
        if not params:
            # TODO: check why this leads to circular import
            from django.db.models.sql.datastructures import EmptyResultSet
            raise EmptyResultSet
        placeholder = '(' + ', '.join('%s' for p in params) + ')'
        return (placeholder, params)

    def get_rhs_op(self, connection, rhs):
        return 'IN %s' % rhs
default_lookups['in'] = In


class PatternLookup(BuiltinLookup):
    def get_rhs_op(self, connection, rhs):
        # Assume we are in startswith. We need to produce SQL like:
        #     col LIKE %s, ['thevalue%']
        # For python values we can (and should) do that directly in Python,
        # but if the value is for example reference to other column, then
        # we need to add the % pattern match to the lookup by something like
        #     col LIKE othercol || '%%'
        # So, for Python values we don't need any special pattern, but for
        # SQL reference values we need the correct pattern added.
        value = self.rhs
        if (hasattr(value, 'get_compiler') or hasattr(value, 'as_sql')
                or hasattr(value, '_as_sql')):
            return connection.pattern_ops[self.lookup_name] % rhs
        else:
            return super(PatternLookup, self).get_rhs_op(connection, rhs)


class StartsWith(PatternLookup):
    lookup_name = 'startswith'
default_lookups['startswith'] = StartsWith


class IStartsWith(PatternLookup):
    lookup_name = 'istartswith'
default_lookups['istartswith'] = IStartsWith


class EndsWith(BuiltinLookup):
    lookup_name = 'endswith'
default_lookups['endswith'] = EndsWith


class IEndsWith(BuiltinLookup):
    lookup_name = 'iendswith'
default_lookups['iendswith'] = IEndsWith


class Between(BuiltinLookup):
    def get_rhs_op(self, connection, rhs):
        return "BETWEEN %s AND %s" % (rhs, rhs)


class Year(Between):
    lookup_name = 'year'
default_lookups['year'] = Year


class Range(Between):
    lookup_name = 'range'
default_lookups['range'] = Range


class DateLookup(BuiltinLookup):

    def process_lhs(self, qn, connection):
        lhs, params = super(DateLookup, self).process_lhs(qn, connection)
        tzname = timezone.get_current_timezone_name() if settings.USE_TZ else None
        sql, tz_params = connection.ops.datetime_extract_sql(self.extract_type, lhs, tzname)
        return connection.ops.lookup_cast(self.lookup_name) % sql, tz_params

    def get_rhs_op(self, connection, rhs):
        return '= %s' % rhs


class Month(DateLookup):
    lookup_name = 'month'
    extract_type = 'month'
default_lookups['month'] = Month


class Day(DateLookup):
    lookup_name = 'day'
    extract_type = 'day'
default_lookups['day'] = Day


class WeekDay(DateLookup):
    lookup_name = 'week_day'
    extract_type = 'week_day'
default_lookups['week_day'] = WeekDay


class Hour(DateLookup):
    lookup_name = 'hour'
    extract_type = 'hour'
default_lookups['hour'] = Hour


class Minute(DateLookup):
    lookup_name = 'minute'
    extract_type = 'minute'
default_lookups['minute'] = Minute


class Second(DateLookup):
    lookup_name = 'second'
    extract_type = 'second'
default_lookups['second'] = Second


class IsNull(BuiltinLookup):
    lookup_name = 'isnull'

    def as_sql(self, qn, connection):
        sql, params = qn.compile(self.lhs)
        if self.rhs:
            return "%s IS NULL" % sql, params
        else:
            return "%s IS NOT NULL" % sql, params
default_lookups['isnull'] = IsNull


class Search(BuiltinLookup):
    lookup_name = 'search'
default_lookups['search'] = Search


class Regex(BuiltinLookup):
    lookup_name = 'regex'
default_lookups['regex'] = Regex


class IRegex(BuiltinLookup):
    lookup_name = 'iregex'
default_lookups['iregex'] = IRegex
