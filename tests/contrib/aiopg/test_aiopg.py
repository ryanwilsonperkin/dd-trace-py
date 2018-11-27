# stdlib
import time
import asyncio

# 3p
import aiopg
from psycopg2 import extras
from nose.tools import eq_

# project
from ddtrace.contrib.aiopg.patch import patch, unpatch
from ddtrace import Pin

# testing
from tests.base import BaseTracerTestCase
from tests.contrib.asyncio.utils import AsyncioTestCase, mark_asyncio
from tests.contrib.config import POSTGRES_CONFIG
from tests.opentracer.utils import init_tracer
from tests.test_tracer import get_dummy_tracer


TEST_PORT = str(POSTGRES_CONFIG['port'])


class TestPsycopgPatch(BaseTracerTestCase, AsyncioTestCase):
    # default service
    TEST_SERVICE = 'postgres'

    def setUp(self):
        super().setUp()
        self._conn = None
        patch()

    def tearDown(self):
        super().tearDown()
        if self._conn and not self._conn.closed:
            self._conn.close()

        unpatch()

    @asyncio.coroutine
    def _get_conn_and_tracer(self):
        conn = self._conn = yield from aiopg.connect(**POSTGRES_CONFIG)
        Pin.get_from(conn).clone(tracer=self.tracer).onto(conn)

        return conn, self.tracer

    @asyncio.coroutine
    def assert_conn_is_traced(self, tracer, db, service):

        # ensure the trace aiopg client doesn't add non-standard
        # methods
        try:
            yield from db.execute('select \'foobar\'')
        except AttributeError:
            pass

        writer = tracer.writer
        # Ensure we can run a query and it's correctly traced
        q = 'select \'foobarblah\''
        start = time.time()
        cursor = yield from db.cursor()
        yield from cursor.execute(q)
        rows = yield from cursor.fetchall()
        end = time.time()
        eq_(rows, [('foobarblah',)])
        assert rows
        spans = writer.pop()
        assert spans
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.name, 'postgres.query')
        eq_(span.resource, q)
        eq_(span.service, service)
        eq_(span.meta['sql.query'], q)
        eq_(span.error, 0)
        eq_(span.span_type, 'sql')
        assert start <= span.start <= end
        assert span.duration <= end - start

        # Ensure OpenTracing compatibility
        ot_tracer = init_tracer('aiopg_svc', tracer)
        with ot_tracer.start_active_span('aiopg_op'):
            cursor = yield from db.cursor()
            yield from cursor.execute(q)
            rows = yield from cursor.fetchall()
            eq_(rows, [('foobarblah',)])
        spans = writer.pop()
        eq_(len(spans), 2)
        ot_span, dd_span = spans
        # confirm the parenting
        eq_(ot_span.parent_id, None)
        eq_(dd_span.parent_id, ot_span.span_id)
        eq_(ot_span.name, 'aiopg_op')
        eq_(ot_span.service, 'aiopg_svc')
        eq_(dd_span.name, 'postgres.query')
        eq_(dd_span.resource, q)
        eq_(dd_span.service, service)
        eq_(dd_span.meta['sql.query'], q)
        eq_(dd_span.error, 0)
        eq_(dd_span.span_type, 'sql')

        # run a query with an error and ensure all is well
        q = 'select * from some_non_existant_table'
        cur = yield from db.cursor()
        try:
            yield from cur.execute(q)
        except Exception:
            pass
        else:
            assert 0, 'should have an error'
        spans = writer.pop()
        assert spans, spans
        eq_(len(spans), 1)
        span = spans[0]
        eq_(span.name, 'postgres.query')
        eq_(span.resource, q)
        eq_(span.service, service)
        eq_(span.meta['sql.query'], q)
        eq_(span.error, 1)
        eq_(span.meta['out.host'], 'localhost')
        eq_(span.meta['out.port'], TEST_PORT)
        eq_(span.span_type, 'sql')

    @mark_asyncio
    def test_execute_metadata(self):
        conn = yield from aiopg.connect(**POSTGRES_CONFIG)
        Pin.get_from(conn).clone(tracer=self.tracer).onto(conn)
        yield from (yield from conn.cursor()).execute('select \'blah\'')
        conn.close()
        span = self.get_root_span()
        span.assert_matches(
            name='postgres.query',
            service='postgres',
            span_type='sql',
            resource='select \'blah\'',
        )

    @mark_asyncio
    def test_disabled_execute(self):
        conn, tracer = yield from self._get_conn_and_tracer()
        tracer.enabled = False
        # these calls were crashing with a previous version of the code.
        yield from (yield from conn.cursor()).execute(query='select \'blah\'')
        yield from (yield from conn.cursor()).execute('select \'blah\'')
        assert not tracer.writer.pop()

    @mark_asyncio
    def test_manual_wrap_extension_types(self):
        conn, _ = yield from self._get_conn_and_tracer()
        # NOTE: this will crash if it doesn't work.
        #   _ext.register_type(_ext.UUID, conn_or_curs)
        #   TypeError: argument 2 must be a connection, cursor or None
        extras.register_uuid(conn_or_curs=conn)

    @mark_asyncio
    def test_patch_unpatch(self):
        tracer = get_dummy_tracer()
        writer = tracer.writer

        # Test patch idempotence
        patch()
        patch()

        service = 'fo'

        conn = yield from aiopg.connect(**POSTGRES_CONFIG)
        Pin.get_from(conn).clone(service=service, tracer=tracer).onto(conn)
        yield from (yield from conn.cursor()).execute('select \'blah\'')
        conn.close()

        spans = writer.pop()
        assert spans, spans
        eq_(len(spans), 1)

        # Test unpatch
        unpatch()

        conn = yield from aiopg.connect(**POSTGRES_CONFIG)
        yield from (yield from conn.cursor()).execute('select \'blah\'')
        conn.close()

        spans = writer.pop()
        assert not spans, spans

        # Test patch again
        patch()

        conn = yield from aiopg.connect(**POSTGRES_CONFIG)
        Pin.get_from(conn).clone(service=service, tracer=tracer).onto(conn)
        yield from (yield from conn.cursor()).execute('select \'blah\'')
        conn.close()

        spans = writer.pop()
        assert spans, spans
        eq_(len(spans), 1)
