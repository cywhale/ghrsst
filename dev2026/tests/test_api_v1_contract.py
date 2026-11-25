"""GHRSST Web API v1 — contract regression tests (spec dev2026/specs/ghrsst_v1/03 Stage D).

What this pins:
  1. Routes are unchanged (paths, methods, docs/openapi URLs); the published operationIds are the
     owner-chosen stable ones, `servers` is the production host, and both operations are public
     (`security: []`, no security scheme).
  2. The generated OpenAPI matches the reviewed snapshot (tests/snapshots/openapi_v1.json),
     has no placeholder public metadata, and every $ref / schema / example is valid.
  3. Real handler responses — every documented status, built over synthetic stores —
     validate against the schema the generated OpenAPI publishes for that status, and the
     documented headers are present (including: Retry-After ONLY on the overload 503).
  4. Current behaviour the contract describes but no older test pinned (spec 01 §15).

Needs the dev/test-only dependency `jsonschema` (dev2026/requirements-test.txt). Without it
every test here is SKIPPED with that reason — never silently passed.

No network, no VM24, no real data. Run:
  dev2026/.venv/bin/python -m unittest dev2026.tests.test_api_v1_contract
Refresh the snapshot after a reviewed contract change:
  GHRSST_UPDATE_OPENAPI_SNAPSHOT=1 dev2026/.venv/bin/python -m unittest dev2026.tests.test_api_v1_contract
"""
from __future__ import annotations

import datetime
import inspect
import json
import re
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from store.zarr_paths import group_path  # noqa: E402

try:
    from jsonschema import Draft202012Validator
    HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover - exercised only in an env without the test dep
    HAVE_JSONSCHEMA = False

NEEDS_JSONSCHEMA = unittest.skipUnless(
    HAVE_JSONSCHEMA, "jsonschema not installed: pip install -r dev2026/requirements-test.txt")

SNAPSHOT = os.path.join(HERE, "snapshots", "openapi_v1.json")
# Read once at import: the tests below clear every GHRSST_* variable while they run.
UPDATE_SNAPSHOT = os.environ.get("GHRSST_UPDATE_OPENAPI_SNAPSHOT") == "1"
GET_PATH, POST_PATH = "/api/ghrsst", "/api/ghrsst/points"

# Code defaults of the config values the contract documents or the tests rely on, applied
# whatever the developer's environment says, so results do not depend on the shell.
CODE_DEFAULT_CFG = {"MAX_DAYS": 366, "POINTS_BATCH_MAX": 1000, "BATCH_CHUNK_FANOUT_MAX": 64,
                    "BBOX_POINT_LIMIT": 1_000_000, "LONG_CACHE_SECONDS": 864000,
                    "SPATIAL_WINDOW_ENFORCE": 0, "ENABLE_COVERAGEJSON": 0}
# What the public service runs with (spec 01 §2). The snapshot is generated with these, so it
# shows what production publishes.
PRODUCTION_LIKE_CFG = dict(CODE_DEFAULT_CFG, BBOX_POINT_LIMIT=300_000, SPATIAL_WINDOW_ENFORCE=1)


# ---- shared app, temporarily configured -----------------------------------------------------
class _ConfiguredApp:
    """Run the SHARED `api.app` module with an exact GHRSST_* environment (read by the lifespan
    when a TestClient starts) and exact `cfg` values, then restore both.

    Deliberately no `importlib.reload`: other test modules hold objects imported from `api.app`
    (e.g. test_api's `Overloaded`), and a reload would swap those classes under them."""

    def __init__(self, env=None, cfg=None):
        self.env = dict(env or {})
        self.cfg = dict(CODE_DEFAULT_CFG, **(cfg or {}))

    def __enter__(self):
        import api.app as appmod
        from api import openapi_contract
        self.mod, self.cache_attr = appmod, openapi_contract.CACHE_ATTR
        self.saved_env = {k: v for k, v in os.environ.items() if k.startswith("GHRSST_")}
        for k in self.saved_env:
            del os.environ[k]
        os.environ.update(self.env)
        self.saved_cfg = dict(vars(appmod.cfg))
        for k, v in self.cfg.items():
            setattr(appmod.cfg, k, v)
        setattr(appmod.app, self.cache_attr, None)      # regenerate the document for this cfg
        return appmod

    def __exit__(self, *exc):
        for k in [k for k in os.environ if k.startswith("GHRSST_")]:
            del os.environ[k]
        os.environ.update(self.saved_env)
        vars(self.mod.cfg).clear()
        vars(self.mod.cfg).update(self.saved_cfg)
        setattr(self.mod.app, self.cache_attr, None)
        return False


# ---- schema helpers ------------------------------------------------------------------------
def schema_validator(doc, schema):
    """Validate against a schema from `doc`, resolving '#/components/...' refs in `doc`."""
    root = dict(schema)
    root["components"] = doc["components"]
    return Draft202012Validator(root)


def schema_errors(doc, schema, instance):
    return sorted(str(e.message) for e in schema_validator(doc, schema).iter_errors(instance))


def walk(node, path=()):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, path + (i,))


def resolve(doc, ref):
    assert ref.startswith("#/"), ref
    node = doc
    for part in ref[2:].split("/"):
        node = node[part]
    return node


class ContractAssertions(unittest.TestCase):
    doc = None          # set by subclasses: the OpenAPI the app under test publishes

    def conform(self, resp, path, method):
        """The response's status is documented for the operation, its body validates against the
        published schema for that status, and every required documented header is present."""
        op = self.doc["paths"][path][method]
        status = str(resp.status_code)
        self.assertIn(status, op["responses"],
                      f"{method.upper()} {path}: undocumented status {status}: {resp.text[:300]}")
        spec = op["responses"][status]
        self.assertTrue(resp.headers.get("content-type", "").startswith("application/json"),
                        resp.headers.get("content-type"))
        body = json.loads(resp.content)
        errs = schema_errors(self.doc, spec["content"]["application/json"]["schema"], body)
        self.assertEqual(errs, [], f"{method.upper()} {path} {status} body does not match: {body!r}")
        for name, h in spec.get("headers", {}).items():
            if h.get("required"):
                self.assertIn(name.lower(), resp.headers, f"required header {name} missing")
            if name.lower() in resp.headers:
                raw = resp.headers[name.lower()]
                kind = h["schema"].get("type")
                value = int(raw) if kind == "integer" else float(raw) if kind == "number" else raw
                self.assertEqual(schema_errors(self.doc, h["schema"], value), [],
                                 f"header {name}={raw!r} does not match {h['schema']}")
        return body


# ---- synthetic stores ----------------------------------------------------------------------
NY, NX = 40, 50
LONS = np.linspace(100.0, 130.0, NX).astype(np.float32)
LATS = np.linspace(0.0, 30.0, NY).astype(np.float32)
DAILY_DAYS = ["2025-01-01", "2025-01-02", "2025-01-03"]
ANOMALY_DAY = "2025-01-02"          # the only day that has sst_anomaly


def build_daily_day(root, day, variables=("sst", "sea_ice"), lon=LONS, lat=LATS):
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(lon.size,), dtype="float32", chunks=(lon.size,))
    g.create_array("lat", shape=(lat.size,), dtype="float32", chunks=(lat.size,))
    g["lon"][:] = lon
    g["lat"][:] = lat
    for v in variables:
        g.create_array(v, shape=(1, lat.size, lon.size), dtype="float32", chunks=(1, 16, 16))
        fld = (10.0 + 0.01 * np.arange(lat.size)[:, None]
               + 0.001 * np.arange(lon.size)[None, :]).astype(np.float32)
        if v == "sst":
            fld[5, 7] = np.nan                     # land
        if v == "sea_ice":
            fld[:] = 0.0
        g[v][0] = fld


# ---- 1/2. the published document ------------------------------------------------------------
@NEEDS_JSONSCHEMA
class OpenApiDocumentTests(ContractAssertions):
    @classmethod
    def setUpClass(cls):
        cls.ctx = _ConfiguredApp(cfg=PRODUCTION_LIKE_CFG)
        cls.appmod = cls.ctx.__enter__()
        cls.doc = cls.appmod.app.openapi()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def test_routes_unchanged(self):
        self.assertEqual({p: sorted(ops) for p, ops in self.doc["paths"].items()},
                         {GET_PATH: ["get"], POST_PATH: ["post"]})
        self.assertEqual(self.doc["paths"][GET_PATH]["get"]["operationId"], "getGhrsstData")
        self.assertEqual(self.doc["paths"][POST_PATH]["post"]["operationId"], "postGhrsstPoints")
        app = self.appmod.app
        self.assertEqual((app.docs_url, app.openapi_url),
                         ("/api/swagger/ghrsst", "/api/swagger/ghrsst/openapi.json"))
        served = {(r.path, m) for r in app.routes if hasattr(r, "methods") for m in r.methods
                  if getattr(r, "include_in_schema", False)}
        self.assertEqual(served, {(GET_PATH, "GET"), (POST_PATH, "POST")})
        self.assertNotIn("/healthz", self.doc["paths"])

    def test_snapshot(self):
        generated = json.loads(json.dumps(self.doc))
        if UPDATE_SNAPSHOT:
            os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
            with open(SNAPSHOT, "w", encoding="utf-8") as fh:
                json.dump(generated, fh, indent=2, sort_keys=True, ensure_ascii=False)
                fh.write("\n")
        with open(SNAPSHOT, encoding="utf-8") as fh:
            self.assertEqual(generated, json.load(fh),
                             "generated OpenAPI differs from tests/snapshots/openapi_v1.json; review "
                             "the change, then refresh with GHRSST_UPDATE_OPENAPI_SNAPSHOT=1")

    def test_no_placeholder_public_metadata(self):
        # OD-11: absent until the owner provides them; never a placeholder. (`servers` is decided,
        # OD-14: see test_servers_operation_ids_and_security.)
        for key in ("contact", "license", "termsOfService"):
            self.assertNotIn(key, self.doc["info"])
        self.assertNotIn("TBD", json.dumps(self.doc))

    def test_servers_operation_ids_and_security(self):
        # Owner decisions: production server (paths already carry /api, so the URL has no /api);
        # stable unique operationIds; public operations with an explicit empty `security`.
        # URL only: clients (APIverse) build request URLs from the server object.
        self.assertEqual(self.doc["servers"], [{"url": "https://eco.odb.ntu.edu.tw"}])
        self.assertEqual(set(self.doc["servers"][0]), {"url"})
        self.assertFalse(self.doc["servers"][0]["url"].rstrip("/").endswith("/api"))
        for path in self.doc["paths"]:
            self.assertTrue(path.startswith("/api/"), path)
        ids = [op["operationId"] for ops in self.doc["paths"].values() for op in ops.values()]
        self.assertEqual(sorted(ids), ["getGhrsstData", "postGhrsstPoints"])
        self.assertEqual(len(ids), len(set(ids)))
        sentence = "This is a public API; no authentication is required."
        for path, method in ((GET_PATH, "get"), (POST_PATH, "post")):
            op = self.doc["paths"][path][method]
            self.assertEqual(op["security"], [], (path, method))
            self.assertEqual(op["description"].count(sentence), 1, (path, method))
            self.assertTrue(op["description"].split("\n")[0].endswith(sentence), (path, method))
        self.assertNotIn("security", self.doc)                       # no global security
        self.assertNotIn("securitySchemes", self.doc.get("components", {}))
        self.assertEqual(self.doc["info"]["version"], "1.0.0")      # bumped at Gate F2 (2026-11-24)

    # --- Round 10 follow-up: the public `security: []` must never hide a generated auth requirement ---
    def _raw(self):
        from fastapi.openapi.utils import get_openapi
        app = self.appmod.app
        return get_openapi(title=app.title, version=app.version, openapi_version=app.openapi_version,
                           description=app.description, routes=app.routes)

    def test_generated_document_has_no_authentication(self):
        raw = self._raw()
        self.assertNotIn("security", raw)
        self.assertNotIn("securitySchemes", raw.get("components", {}))
        for path, method in ((GET_PATH, "get"), (POST_PATH, "post")):
            self.assertNotIn("security", raw["paths"][path][method])
            self.assertEqual(self.doc["paths"][path][method]["security"], [])
        self.assertNotIn("security", self.doc)
        self.assertNotIn("securitySchemes", self.doc["components"])

    def test_contract_refuses_to_hide_authentication(self):
        from api import openapi_contract as oc
        cases = {
            "root": lambda d: d.__setitem__("security", [{"apiKey": []}]),
            # anonymous OR bearer (OAS 3.1.1 §4.8.30): a real scheme is present -> still refused
            "root anonymous + bearer": lambda d: d.__setitem__("security", [{}, {"bearerAuth": []}]),
            "GET operation": lambda d: d["paths"][GET_PATH]["get"].__setitem__(
                "security", [{"apiKey": []}]),
            "GET anonymous + apiKey": lambda d: d["paths"][GET_PATH]["get"].__setitem__(
                "security", [{}, {"apiKey": []}]),
            "POST operation": lambda d: d["paths"][POST_PATH]["post"].__setitem__(
                "security", [{"oauth": ["read"]}]),
            "securitySchemes": lambda d: d["components"].__setitem__(
                "securitySchemes", {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Key"}}),
            # malformed values: refused by key presence + type, never skipped as falsey (Codex P2)
            "root security null": lambda d: d.__setitem__("security", None),
            "root security not a list": lambda d: d.__setitem__("security", {}),
            "GET security null": lambda d: d["paths"][GET_PATH]["get"].__setitem__("security", None),
            "POST security list of non-objects": lambda d: d["paths"][POST_PATH]["post"].__setitem__(
                "security", ["apiKey"]),
            "securitySchemes null": lambda d: d["components"].__setitem__("securitySchemes", None),
            "securitySchemes []": lambda d: d["components"].__setitem__("securitySchemes", []),
            # a non-object `components` is refused, never treated as absent (regression)
            "components null": lambda d: d.__setitem__("components", None),
            "components []": lambda d: d.__setitem__("components", []),
        }
        for name, inject in cases.items():
            with self.subTest(case=name):
                raw = self._raw()
                inject(raw)
                before = json.loads(json.dumps(raw))
                with self.assertRaises(oc.OpenApiSecurityConflict) as cm:
                    oc.apply_contract(raw, self.appmod.cfg)
                self.assertIn("refusing to publish `security: []`", str(cm.exception))
                self.assertEqual(raw, before)          # nothing deleted or overwritten
        # the served-document path refuses too: no public `security: []` document is produced
        app, attr = self.appmod.app, oc.CACHE_ATTR
        real_generate = app.openapi
        try:
            raw = self._raw()
            raw["paths"][GET_PATH]["get"]["security"] = [{"apiKey": []}]
            app.openapi = lambda: raw                  # stand-in for FastAPI's generator
            oc.install(app, self.appmod.cfg)
            with self.assertRaises(oc.OpenApiSecurityConflict):
                app.openapi()
            self.assertIsNone(getattr(app, attr))
        finally:
            app.openapi = real_generate
            setattr(app, attr, None)

    def test_anonymous_only_security_is_normalised(self):
        # `[]` and `[{}]` (anonymous access suffices, OAS 3.1.1 §4.8.30) with no security scheme are
        # accepted and published as the contract's operation-level `security: []`; an empty
        # `securitySchemes: {}` is accepted and dropped.
        from api import openapi_contract as oc
        for label, root, op_sec, schemes in (("root []", [], None, None),
                                             ("root [{}]", [{}], None, None),
                                             ("operation [{}]", None, [{}], None),
                                             ("empty securitySchemes {}", None, None, {})):
            with self.subTest(case=label):
                raw = self._raw()
                if root is not None:
                    raw["security"] = root
                if op_sec is not None:
                    raw["paths"][GET_PATH]["get"]["security"] = op_sec
                    raw["paths"][POST_PATH]["post"]["security"] = list(op_sec)
                if schemes is not None:
                    raw["components"]["securitySchemes"] = schemes
                doc = oc.apply_contract(raw, self.appmod.cfg)
                self.assertNotIn("security", doc)
                self.assertNotIn("securitySchemes", doc["components"])
                for path, method in ((GET_PATH, "get"), (POST_PATH, "post")):
                    self.assertEqual(doc["paths"][path][method]["security"], [])

    def test_version_is_the_approved_contract(self):
        # Gate F2 (owner, 2026-11-24) approved the public contract; OD-20 bumps info.version to
        # 1.0.0 at that point. This is the OpenAPI contract version, not the service release (ver 0.x).
        self.assertEqual(self.doc["info"]["version"], "1.0.0")
        self.assertEqual(self.doc["openapi"], "3.1.0")

    def test_every_ref_resolves_and_schemas_are_valid(self):
        for path, node in walk(self.doc):
            if "$ref" in node:
                self.assertTrue(node["$ref"].startswith("#/components/schemas/"), (path, node))
                resolve(self.doc, node["$ref"])
            self.assertNotIn("allOf", node, f"allOf at {path}: extending a closed schema is unsafe")
        for name, schema in self.doc["components"]["schemas"].items():
            Draft202012Validator.check_schema(schema)

    def test_422_is_frameworks_validation_error(self):
        # Spec 02 §4: both operations reference the same FastAPI components, transcribed field by field.
        # Round 7: only the 422 *description* is rewritten (error reasons live with the response);
        # the content stays FastAPI's own `$ref` (no new error format).
        for path, method in ((GET_PATH, "get"), (POST_PATH, "post")):
            r422 = self.doc["paths"][path][method]["responses"]["422"]
            self.assertEqual(set(r422), {"description", "content"})
            self.assertEqual(r422["content"], {"application/json": {"schema": {
                "$ref": "#/components/schemas/HTTPValidationError"}}})
            self.assertIn("not a finite number", r422["description"])
        s = self.doc["components"]["schemas"]
        self.assertEqual(s["HTTPValidationError"], {
            "title": "HTTPValidationError", "type": "object",
            "properties": {"detail": {"title": "Detail", "type": "array",
                                      "items": {"$ref": "#/components/schemas/ValidationError"}}}})
        self.assertEqual(s["ValidationError"], {
            "title": "ValidationError", "type": "object", "required": ["loc", "msg", "type"],
            "properties": {
                "loc": {"title": "Location", "type": "array",
                        "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
                "msg": {"title": "Message", "type": "string"},
                "type": {"title": "Error Type", "type": "string"},
                "input": {"title": "Input"},
                "ctx": {"title": "Context", "type": "object"}}})

    def test_documented_statuses(self):
        for path, method in ((GET_PATH, "get"), (POST_PATH, "post")):
            self.assertEqual(sorted(self.doc["paths"][path][method]["responses"]),
                             ["200", "400", "413", "422", "503"])

    def test_retry_after_is_optional_and_overload_only(self):
        for path, method in ((GET_PATH, "get"), (POST_PATH, "post")):
            r503 = self.doc["paths"][path][method]["responses"]["503"]
            self.assertIs(r503["headers"]["Retry-After"]["required"], False)
            self.assertIn("ONLY", r503["headers"]["Retry-After"]["description"])
        self.assertEqual(sorted(self.doc["paths"][GET_PATH]["get"]["responses"]["503"]["content"]
                                ["application/json"]["examples"]),
                         ["no_available_dates", "no_available_spatial_dates", "overloaded"])

    def test_every_example_matches_its_schema(self):
        n = 0
        for path, node in walk(self.doc["paths"]):
            if "schema" in node and "examples" in node:
                for name, ex in node["examples"].items():
                    self.assertEqual(schema_errors(self.doc, node["schema"], ex["value"]), [],
                                     f"example {name} at {path}")
                    n += 1
        self.assertGreaterEqual(n, 15)

    def test_documented_request_matches_the_model(self):
        # The documented PointsRequest replaces pydantic's; it must not drift from the model.
        model = self.appmod.PointsRequest
        documented = self.doc["components"]["schemas"]["PointsRequest"]
        self.assertEqual(set(documented["properties"]), set(model.model_fields))
        self.assertEqual(set(documented["required"]),
                         {n for n, f in model.model_fields.items() if f.is_required()})
        self.assertEqual(documented["properties"]["points"]["maxItems"], self.appmod.cfg.POINTS_BATCH_MAX)

    def test_append_default_is_sst_and_stays_optional(self):
        # Owner 2026-11-20: `append` documents `default: "sst"` in both operations; it stays
        # optional, and `mode` is left as it was (no default, no enum).
        params = {p["name"]: p for p in self.doc["paths"][GET_PATH]["get"]["parameters"]}
        body = self.doc["components"]["schemas"]["PointsRequest"]
        self.assertEqual(params["append"]["schema"]["default"], "sst")
        self.assertEqual(body["properties"]["append"]["default"], "sst")
        self.assertIs(params["append"]["required"], False)
        self.assertEqual(body["required"], ["date", "points"])
        self.assertNotIn("append", body["required"])
        for schema in (params["append"]["schema"], body["properties"]["append"]):
            self.assertNotIn("enum", schema)
        for schema in (params["mode"]["schema"], body["properties"]["mode"]):
            self.assertNotIn("default", schema)
            self.assertNotIn("enum", schema)
        self.assertIs(params["mode"]["required"], False)
        # the default is a valid value of both schemas
        self.assertEqual(schema_errors(self.doc, params["append"]["schema"], "sst"), [])
        self.assertEqual(schema_errors(self.doc, body["properties"]["append"], "sst"), [])

    def test_documented_parameters_match_the_handler(self):
        handler = set(inspect.signature(self.appmod.read_ghrsst).parameters) - {"request"}
        params = self.doc["paths"][GET_PATH]["get"]["parameters"]
        self.assertEqual({p["name"] for p in params}, handler)
        required = {p["name"] for p in params if p.get("required")}
        self.assertEqual(required, {"lon0", "lat0"})

    def test_format_is_not_documented_as_a_strict_enum(self):
        # The handler trims + lower-cases `format`, so `JSON` / ` json ` are valid inputs; an
        # `enum: ["json"]` would wrongly document them as invalid (Codex round-4 review).
        p = next(p for p in self.doc["paths"][GET_PATH]["get"]["parameters"] if p["name"] == "format")
        self.assertNotIn("enum", p["schema"])
        self.assertEqual((p["schema"]["type"], p["schema"]["default"]), ("string", "json"))
        self.assertEqual(p["description"],
                         "Only JSON is supported (case-insensitive). Omitted or empty means JSON.")
        for value in ("json", "JSON", " json ", " JSON\t"):
            self.assertEqual(schema_errors(self.doc, p["schema"], value), [], value)

    def test_date_schemas_reject_a_trailing_newline(self):
        # Codex round-5 review: `$` in the pattern also matches before a trailing newline, so the
        # pattern alone documented "2025-01-01\n" as valid while BC-1 answers it with 400.
        params = {p["name"]: p for p in self.doc["paths"][GET_PATH]["get"]["parameters"]}
        schemas = {"Date": {"$ref": "#/components/schemas/Date"},
                   "start": params["start"]["schema"], "end": params["end"]["schema"]}
        self.assertEqual(self.doc["components"]["schemas"]["Date"]["maxLength"], 10)
        for name, schema in schemas.items():
            with self.subTest(schema=name):
                self.assertEqual(schema_errors(self.doc, schema, "2025-01-01"), [])
                self.assertNotEqual(schema_errors(self.doc, schema, "2025-01-01\n"), [])
                self.assertNotEqual(schema_errors(self.doc, schema, "2025-01-011"), [])
        for name in ("start", "end"):
            string_branch = next(b for b in params[name]["schema"]["anyOf"] if b.get("type") == "string")
            self.assertEqual((string_branch["maxLength"], string_branch["pattern"]),
                             (10, r"^\d{4}-\d{2}-\d{2}$"))

    def test_contract_rewrites_documentation_only(self):
        # Parameter location / requiredness / type and the request body binding are FastAPI's,
        # untouched by the contract module (it adds documentation keywords only).
        from fastapi.openapi.utils import get_openapi
        app = self.appmod.app
        raw = get_openapi(title=app.title, version=app.version, openapi_version=app.openapi_version,
                          description=app.description, routes=app.routes)

        def shape(doc):
            get = doc["paths"][GET_PATH]["get"]
            post = doc["paths"][POST_PATH]["post"]
            return ([(p["name"], p["in"], p.get("required"), p["schema"].get("type"),
                      [b.get("type") for b in p["schema"].get("anyOf", [])],
                      p["schema"].get("default")) for p in get["parameters"]],
                    post["requestBody"]["required"],
                    post["requestBody"]["content"]["application/json"]["schema"])
        # The one schema default the contract adds (owner, 2026-11-20): `append` documents its
        # effective default "sst" (the handler itself takes None and returns sst). Nothing else moves.
        expected = shape(raw)
        expected = ([t[:5] + ("sst",) if t[0] == "append" else t for t in expected[0]],) + expected[1:]
        self.assertEqual(shape(self.doc), expected)
        self.assertIsNone(next(p for p in raw["paths"][GET_PATH]["get"]["parameters"]
                               if p["name"] == "append")["schema"].get("default"))
        # operationIds are the only identifiers the contract replaces (owner decision); the raw
        # FastAPI ones remain what the framework generates from the handler names.
        self.assertEqual((raw["paths"][GET_PATH]["get"]["operationId"],
                          raw["paths"][POST_PATH]["post"]["operationId"]),
                         ("read_ghrsst_api_ghrsst_get", "read_points_api_ghrsst_points_post"))
        self.assertNotIn("servers", raw)

    def test_limits_come_from_deployment_config(self):
        get_desc = self.doc["paths"][GET_PATH]["get"]["description"]
        self.assertIn("at most 300000 grid cells", get_desc)     # production-like GHRSST_BBOX_POINT_LIMIT
        self.assertIn("at most 366 inclusive calendar dates", get_desc)

    # --- Round 7: owner-chosen data-date windows, and where each rule lives ---
    def _ops(self):
        return {"get": self.doc["paths"][GET_PATH]["get"], "post": self.doc["paths"][POST_PATH]["post"]}

    GET_HEADS = ("**Coordinates**", "**Query type**", "**Point Query**", "**Bounding-Box Query**",
                 "**Data freshness**")

    def _section(self, desc, head):
        """Text of one GET description section: from its heading to the next heading."""
        i = desc.index(head)
        later = [desc.index(h) for h in self.GET_HEADS if h in desc and desc.index(h) > i]
        return desc[i:min(later)] if later else desc[i:]

    def test_service_windows_are_documented_per_endpoint(self):
        from api import openapi_contract as oc
        self.assertEqual((oc.SPATIAL_WINDOW_DAYS, oc.POINT_WINDOW_DAYS), (31, 366))
        # Round 12 (owner-accepted concise wording)
        point = ("Coverage: point queries support the latest 366 consecutive calendar days ending on "
                 "the latest date available to point queries. The archive currently starts on "
                 "2023-01-01.")
        spatial = ("Coverage: bounding-box queries support the latest 31 consecutive calendar days "
                   "ending on the latest date available to spatial queries. The same window applies "
                   "to `POST /api/ghrsst/points`.")
        post = ("Coverage: this endpoint supports the latest 31 consecutive calendar days ending on "
                "the latest date available to spatial queries. This is the same window as a "
                "Bounding-Box Query on `GET /api/ghrsst`.")
        get_desc, post_desc = self._ops()["get"]["description"], self._ops()["post"]["description"]
        point_sec, bbox_sec = self._section(get_desc, "**Point Query**"), self._section(get_desc, "**Bounding-Box Query**")
        # GET: point window in the Point Query section, spatial window in the Bounding-Box section
        self.assertIn(point, point_sec)
        self.assertNotIn("latest 31", point_sec)
        self.assertNotIn("spatial queries", point_sec)
        self.assertIn(spatial, bbox_sec)
        self.assertNotIn("366", bbox_sec)
        self.assertNotIn("point queries", bbox_sec)
        # POST: spatial window only, stated once
        self.assertIn(post, post_desc)
        self.assertEqual(post_desc.count("consecutive calendar days"), 1)
        self.assertNotIn("366", post_desc)
        self.assertNotIn("point date", post_desc)
        for desc in (get_desc, post_desc):
            for wrong in ("exactly 31", "exactly 366", "only 31", "only 366", "ending today"):
                self.assertNotIn(wrong, desc)
        # spatial_window_days stays a retention target, never the current window length
        swd = (self.doc["components"]["schemas"]["SpatialWindowError"]["properties"]["detail"]
               ["properties"]["spatial_window_days"]["description"])
        self.assertIn("Retention target", swd)
        self.assertIn("not the length of the window currently served", swd)

    def test_service_windows_do_not_follow_request_caps(self):
        # The 366-day point window is a contract constant; the 366-day range cap is a deployment value.
        from fastapi.openapi.utils import get_openapi
        from api import openapi_contract as oc
        app = self.appmod.app
        raw = get_openapi(title=app.title, version=app.version, openapi_version=app.openapi_version,
                          description=app.description, routes=app.routes)
        import types
        real = self.appmod.cfg
        attrs = {k: getattr(real, k) for k in dir(real) if k.isupper()}
        attrs.update(MAX_DAYS=10, POINTS_BATCH_MAX=7)
        cfg = types.SimpleNamespace(**attrs)
        doc = oc.apply_contract(raw, cfg)
        get_desc = doc["paths"][GET_PATH]["get"]["description"]
        self.assertIn("at most 10 inclusive calendar dates", get_desc)
        self.assertIn("latest 366 consecutive calendar days", get_desc)
        self.assertIn("The archive currently starts on 2023-01-01.", get_desc)
        self.assertIn("more than 10 inclusive calendar dates", doc["paths"][GET_PATH]["get"]["responses"]["413"]["description"])
        self.assertIn("latest 31 consecutive", doc["paths"][POST_PATH]["post"]["description"])

    def test_request_cap_is_distinct_from_coverage_window(self):
        # Round 12: the 366-day point coverage window and the (currently also 366) per-request cap
        # are different things and must be described as such, in separate bullets.
        point_sec = self._section(self._ops()["get"]["description"], "**Point Query**")
        bullets = {b.split(":", 1)[0]: b for b in point_sec.split("\n* ")[1:]}
        self.assertIn("consecutive calendar days", bullets["Coverage"])
        self.assertNotIn("Request limit", bullets["Coverage"])
        cap = bullets["Request limit"]
        self.assertIn(f"at most {self.appmod.cfg.MAX_DAYS} inclusive calendar dates", cap)
        self.assertIn("This limits the length of one request, not the coverage window above", cap)
        self.assertNotIn("consecutive", cap)
        # every request-cap statement says it is not a coverage window
        from api import openapi_contract as oc
        self.assertTrue(oc.NOT_A_QUOTA.endswith("not a rate limit, a quota or a data coverage window."))

    FORBIDDEN_WINDOW_WORDING = ("guarantee", "no date missing", "under normal service",
                                "earlier dates may be available", "may have gaps",
                                "both ends included")
    INTERNAL_OPS_WORDS = re.compile(r"\b(compaction|replacement|replace[sd]?|audit(s|ed|ing)?|"
                                    r"prune[sd]?|pruning|monitoring|maintenance)\b", re.I)

    def _date_window_texts(self):
        """Only the OAS text that describes the data-date windows (named locations), so the
        wording checks below do not ban ordinary words elsewhere in the document."""
        get_desc, post_desc = self._ops()["get"]["description"], self._ops()["post"]["description"]
        for head in ("**Point Query**", "**Bounding-Box Query**", "**Data freshness**"):
            yield ("get", head), self._section(get_desc, head)
        i, j = post_desc.index("**Date**"), post_desc.index("**Coordinates**")
        yield ("post", "**Date**"), post_desc[i:j]
        detail = self.doc["components"]["schemas"]["SpatialWindowError"]["properties"]["detail"]
        for name, prop in detail["properties"].items():
            if "description" in prop:
                yield ("SpatialWindowError", name), prop["description"]
        body = self.doc["paths"][POST_PATH]["post"]["requestBody"]["content"]["application/json"]
        yield ("post", "requestBody example"), body["examples"]["batch"]["summary"]
        # 400 responses: only the sentences about the spatial window, not the bbox cell cap
        for method, op in self._ops().items():
            for sentence in re.split(r"(?<=[.;])\s+|\n", op["responses"]["400"]["description"]):
                if re.search(r"window", sentence, re.I):
                    yield (method, "400"), sentence

    def test_no_absolute_or_internal_window_wording(self):
        # Round 12: the date-window descriptions carry no absolute / redundant promises and no
        # internal P5 operations. Scoped to those descriptions only (Codex review).
        texts = list(self._date_window_texts())
        joined = "\n".join(t for _, t in texts)
        self.assertEqual(joined.count("consecutive calendar days"), 3)    # the scope is not empty
        self.assertIn("SpatialWindowError", joined)
        for where, text in texts:
            for wrong in self.FORBIDDEN_WINDOW_WORDING:
                self.assertNotIn(wrong, text.lower(), (where, wrong))
            self.assertIsNone(self.INTERNAL_OPS_WORDS.search(text), (where, text))
        # the spatial-window error detail stays on the existing 400 responses
        for op in self._ops().values():
            self.assertIn("SpatialWindowError", op["responses"]["400"]["description"])

    def test_no_global_contract_notes(self):
        info = self.doc["info"]["description"]
        self.assertNotIn("Contract notes", info)
        self.assertIn("https://doi.org/10.5067/GHGMR-4FJ04", info)
        for rule in ("366", "422", "Retry-After", "deployment", "detail", "Swagger UI"):
            self.assertNotIn(rule, info)
        # the small-bbox reminder lives in the GET "Bounding box" section
        get_desc = self._ops()["get"]["description"]
        self.assertIn("In Swagger UI, keep boxes small", self._section(get_desc, "**Bounding-Box Query**"))
        self.assertEqual(get_desc.count("Swagger UI"), 1)

    # --- Round 8: the consecutive-window statements must not be contradicted ---
    GAP_WORDS = re.compile(r"\b(skipped|gaps?|missing|not guaranteed to be complete)\b", re.I)

    def test_gap_wording_never_contradicts_the_consecutive_windows(self):
        # Round 12 (owner): the windows are stated once, as consecutive calendar days, with no
        # qualifier about earlier dates or gaps. So the date-window descriptions may not admit
        # skipped / missing dates or gaps at all (round 8 still allowed "older/earlier" qualifiers).
        # Scoped to the date-window descriptions only (Codex review), like the wording test above.
        texts = list(self._date_window_texts())
        self.assertEqual("\n".join(t for _, t in texts).count("consecutive calendar days"), 3)
        for where, text in texts:
            self.assertIsNone(self.GAP_WORDS.search(text), f"gap wording at {where}: {text!r}")
        aw = (self.doc["components"]["schemas"]["SpatialWindowError"]["properties"]["detail"]
              ["properties"]["available_spatial_window"]["description"])
        self.assertEqual(aw, "First and last date of the spatial data currently served, as "
                             "\"YYYY-MM-DD..YYYY-MM-DD\", or \"(none)\".")

    def test_window_end_dates_match_the_query_kind(self):
        get_desc, post_desc = self._ops()["get"]["description"], self._ops()["post"]["description"]
        self.assertNotIn("newest available product date", get_desc + post_desc)   # ambiguous form
        point_sec = self._section(get_desc, "**Point Query**")
        bbox_sec = self._section(get_desc, "**Bounding-Box Query**")
        self.assertIn("ending on the latest date available to point queries", point_sec)
        self.assertIn("ending on the latest date available to spatial queries", bbox_sec)
        self.assertIn("ending on the latest date available to spatial queries", post_desc)
        # Round 9 (owner): the newest dates currently coincide; no general "they can differ" note.
        for desc in (get_desc, post_desc):
            self.assertNotIn("can differ", desc)
            self.assertNotIn("each have their own newest", desc)

    def test_bbox_dates_are_described_exactly(self):
        # Handler: `start` or `end` alone selects the day; both must be equal; date-less = newest
        # daily date, then the spatial-window check, no fallback (SpatialWindowTests pin this).
        bbox_sec = self._section(self._ops()["get"]["description"], "**Bounding-Box Query**")
        self.assertIn("Dates: one date per request. Give `start` or `end` alone, or both with the "
                      "same date.", bbox_sec)
        self.assertIn("Without a date, the newest date in the daily data is used; if that date is not "
                      "yet available to spatial queries, the request fails instead of falling back to "
                      "an earlier date.", bbox_sec)
        params = {p["name"]: p["description"] for p in self._ops()["get"]["parameters"]}
        self.assertIn("Bounding-Box Query: the query date.", params["start"])
        self.assertIn("Bounding-Box Query: the query date; if `start` is also given, it must be the "
                      "same date.", params["end"])
        for name in ("start", "end"):
            self.assertNotIn("first date", params[name])
            self.assertNotIn("Last date", params[name])
        point_sec = self._section(self._ops()["get"]["description"], "**Point Query**")
        # Stage E (E6): Dates and Coverage name the same end date with the same phrase.
        self.assertIn("Dates: without `start` and `end`, the latest date available to point queries "
                      "is used. With only one of them, that single date. With both, every date in the "
                      "inclusive range; the two dates may be given in either order.", point_sec)
        self.assertNotIn("newest point date", point_sec)
        self.assertEqual(point_sec.count("the latest date available to point queries"), 2)

    def test_point_coverage_is_current_not_permanent(self):
        from api import openapi_contract as oc
        self.assertEqual(oc.POINT_COVERAGE_START, "2023-01-01")
        get_desc = self._ops()["get"]["description"]
        point_sec = self._section(get_desc, "**Point Query**")
        self.assertIn("The archive currently starts on 2023-01-01.", point_sec)
        self.assertEqual(json.dumps(self.doc).count("2023-01-01"), 1)       # stated once, in GET
        for wrong in ("always", "permanent", "since 2023-01-01", "guaranteed from 2023",
                      "every date from 2023"):
            self.assertNotIn(wrong, get_desc.lower())

    def test_freshness_note(self):
        from api import openapi_contract as oc
        fresh = ("The newest product date typically lags 1–2 days behind today, depending on upstream "
                 "GHRSST data availability.")
        self.assertEqual(oc.FRESHNESS, fresh)
        get_desc, post_desc = self._ops()["get"]["description"], self._ops()["post"]["description"]
        # one short note per operation, none elsewhere
        self.assertEqual(get_desc.count(fresh), 1)
        self.assertEqual(post_desc.count(fresh), 1)
        self.assertEqual(json.dumps(self.doc, ensure_ascii=False).count("typically lags"), 2)
        self.assertIn(f"**Data freshness** {fresh} Omit date parameters to request the latest "
                      "available data.", get_desc)
        # not a schedule promise
        everything = json.dumps(self.doc, ensure_ascii=False).lower()
        for word in ("every night", "overnight", "next morning", "cron", "schedule", "updated daily at",
                     "utc", "can lag behind today"):
            self.assertNotIn(word, everything)
        self.assertNotIn("**Data date windows**", get_desc)

    def test_bbox_cell_cap_follows_the_deployment_value(self):
        # VM24 runs 300000, the code default is 1000000: neither may be hard-coded in the document.
        from fastapi.openapi.utils import get_openapi
        from api import openapi_contract as oc
        import types
        app = self.appmod.app
        real = self.appmod.cfg
        for cap in (300_000, 1_000_000, 12_345):
            with self.subTest(cap=cap):
                raw = get_openapi(title=app.title, version=app.version,
                                  openapi_version=app.openapi_version,
                                  description=app.description, routes=app.routes)
                attrs = {k: getattr(real, k) for k in dir(real) if k.isupper()}
                attrs["BBOX_POINT_LIMIT"] = cap
                doc = oc.apply_contract(raw, types.SimpleNamespace(**attrs))
                get = doc["paths"][GET_PATH]["get"]
                self.assertIn(f"at most {cap} grid cells after thinning", get["description"])
                self.assertIn(f"larger than {cap} grid cells", get["responses"]["400"]["description"])
                ex = get["responses"]["400"]["content"]["application/json"]["examples"]["bbox_too_large"]
                self.assertEqual(ex["value"]["detail"],
                                 f"bbox too large (16008001 > {cap}); increase stride or shrink bbox")
                text = json.dumps(doc)
                for other in {300000, 1000000} - {cap}:
                    self.assertNotIn(str(other), text)

    def test_fan_out_example_keeps_runtime_body_and_explains_it(self):
        ex = (self._ops()["post"]["responses"]["413"]["content"]["application/json"]["examples"]["fan_out"])
        fanout = self.appmod.cfg.BATCH_CHUNK_FANOUT_MAX
        self.assertEqual(ex["value"], {"detail": f"batch spans {fanout + 1} chunks > fan-out limit "
                                                 f"{fanout}; shrink/split the request"})
        self.assertEqual(ex["summary"], "Points cover too many areas: split them into smaller "
                                        "batches of points that are close together")

    def test_swagger_grouping_unchanged(self):
        # Owner: keep Swagger's `default` group and expansion; no tags, no UI settings.
        self.assertNotIn("tags", self.doc)
        for op in self._ops().values():
            self.assertNotIn("tags", op)
        self.assertIsNone(self.appmod.app.swagger_ui_parameters)

    def test_get_description_layout(self):
        d = self._ops()["get"]["description"]
        for gone in ("**Mode**", "Point and point time series", "point time series", "**Data date windows**",
                     "**Bounding box**"):
            self.assertNotIn(gone, d)
        self.assertNotIn("point time series", self._ops()["get"]["summary"])
        first = d.split("\n\n")[0]
        self.assertTrue(first.startswith("Query daily sea-surface temperature at a single point"))
        self.assertIn("bounding box (one date)", first)
        positions = [d.index(h) for h in self.GET_HEADS]
        self.assertEqual(positions, sorted(positions))          # Coordinates before Query type, ...
        self.assertIn("**Query type** The request is a Bounding-Box Query when both `lon1` and `lat1` "
                      "are given", d)
        point_sec = self._section(d, "**Point Query**")
        bbox_sec = self._section(d, "**Bounding-Box Query**")
        for sec, labels in ((point_sec, ("Dates:", "Coverage:", "Request limit:")),
                            (bbox_sec, ("Dates:", "Coverage:", "Request limit:"))):
            positions = [sec.index("* " + label) for label in labels]
            self.assertEqual(positions, sorted(positions))
        # variable presence (absent -> left out / null) lives with DataRow and the 200 response
        for sec in (point_sec, bbox_sec):
            self.assertNotIn("does not exist", sec)
        self.assertIn("left out of point rows and is null in bounding-box rows",
                      self.doc["components"]["schemas"]["DataRow"]["description"])
        self.assertIn("point_field_absent", self._ops()["get"]["responses"]["200"]["content"]
                      ["application/json"]["examples"])

    def test_rules_are_stated_once_and_in_the_right_place(self):
        ops = self._ops()
        for name, op in ops.items():
            d = op["description"]
            self.assertEqual(d.count("NaN, Infinity and -Infinity are rejected"), 1, name)
            self.assertIsNone(re.search(r"->\s*\d{3}", d), name)         # no "-> 4xx" shorthand
            self.assertNotIn("Contract notes", d)
            for p in op.get("parameters", []):
                self.assertNotIn("NaN", p["description"])
                self.assertNotIn("clamp", p["description"])
                self.assertIsNone(re.search(r"->|\b4\d\d\b", p["description"]), p["name"])
            # status codes and error formats live in the responses
            for status in ("400", "413", "422"):
                self.assertNotIn("headers", op["responses"][status], (name, status))
        for name, schema in self.doc["components"]["schemas"].items():
            for path, node in walk(schema):
                if isinstance(node.get("description"), str):
                    self.assertIsNone(re.search(r"->\s*\d{3}", node["description"]), (name, path))
        # POST: plain language, no internal storage terms in the main description
        post = ops["post"]["description"]
        self.assertNotIn("chunk", post)
        self.assertNotIn(str(self.appmod.cfg.BATCH_CHUNK_FANOUT_MAX), post)
        self.assertIn("zero-based position", post)
        self.assertIn("duplicate points are kept", post)
        self.assertIn("split them into smaller batches by geographic area", post)

    def test_request_caps_live_with_their_endpoint_and_response(self):
        cfg = self.appmod.cfg
        get, post = self._ops()["get"], self._ops()["post"]
        not_quota = "not a rate limit, a quota or a data coverage window"
        r413 = get["responses"]["413"]["description"]
        self.assertIn(f"more than {cfg.MAX_DAYS} inclusive calendar dates", r413)
        self.assertIn(not_quota, r413)
        r400 = get["responses"]["400"]["description"]
        self.assertIn(f"larger than {cfg.BBOX_POINT_LIMIT} grid cells", r400)
        self.assertIn(not_quota, r400)
        self.assertIn("SpatialWindowError", r400)
        p413 = post["responses"]["413"]["description"]
        self.assertIn(f"more than {cfg.POINTS_BATCH_MAX} points", p413)
        self.assertIn(f"more than {cfg.BATCH_CHUNK_FANOUT_MAX} grid blocks", p413)
        self.assertIn("current deployment value", p413)
        self.assertIn("by geographic area", p413)
        self.assertIn(not_quota, p413)
        self.assertIn("SpatialWindowError", post["responses"]["400"]["description"])
        self.assertIn("written as the strings", post["responses"]["422"]["description"])
        # headers stay on their responses
        self.assertEqual(set(get["responses"]["200"]["headers"]),
                         {"Cache-Control", "X-Store-Route", "X-Served-Rows", "X-Stride",
                          "X-Bbox-Format", "X-Read-Ms"})
        self.assertEqual(set(post["responses"]["200"]["headers"]), {"Cache-Control"})
        for op in (get, post):
            self.assertEqual(set(op["responses"]["503"]["headers"]), {"Retry-After"})
        # date-example reminders sit with each endpoint / example
        self.assertIn("Dates in the examples are illustrative", get["description"])
        self.assertIn("example date is illustrative", post["description"])
        self.assertIn("Illustrative date", post["requestBody"]["content"]["application/json"]
                      ["examples"]["batch"]["summary"])

    def test_validator_catches_the_round1_schema_errors(self):
        # Meta-test: the machinery used above rejects the two schema mistakes found in review —
        # overlapping oneOf branches, and allOf-extending a closed schema.
        row = {"lon": 1.0, "lat": 2.0, "date": "2026-01-01", "sst": 1.0}
        closed = self.doc["components"]["schemas"]["DataRow"]
        overlapping = {"oneOf": [closed, dict(closed)]}
        self.assertNotEqual(schema_errors(self.doc, overlapping, row), [])
        extended = {"allOf": [closed, {"properties": {"index": {"type": "integer"}}}]}
        self.assertNotEqual(schema_errors(self.doc, extended, dict(row, index=0)), [])
        self.assertEqual(schema_errors(self.doc, {"$ref": "#/components/schemas/DataRow"}, row), [])


# ---- 3/4. handler behaviour vs the published document (daily-only store) --------------------
@NEEDS_JSONSCHEMA
class DailyStoreConformanceTests(ContractAssertions):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghrsst_v1_")
        for d in DAILY_DAYS:
            build_daily_day(cls.tmp, d, variables=("sst", "sea_ice", "sst_anomaly")
                            if d == ANOMALY_DAY else ("sst", "sea_ice"))
        cls.ctx = _ConfiguredApp(env={"GHRSST_ZARR_PATH": cls.tmp})
        cls.appmod = cls.ctx.__enter__()
        from fastapi.testclient import TestClient
        cls.cm = TestClient(cls.appmod.app, raise_server_exceptions=False)
        cls.client = cls.cm.__enter__()
        cls.doc = cls.appmod.app.openapi()

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)
        cls.ctx.__exit__(None, None, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, **params):
        return self.client.get(GET_PATH, params=params)

    def post(self, body):
        return self.client.post(POST_PATH, json=body)

    def assert_get(self, status, **params):
        r = self.get(**params)
        self.assertEqual(r.status_code, status, r.text[:300])
        return r, self.conform(r, GET_PATH, "get")

    def assert_post(self, status, body):
        r = self.post(body)
        self.assertEqual(r.status_code, status, r.text[:300])
        return r, self.conform(r, POST_PATH, "post")

    # --- served document ---
    def test_openapi_served_over_http_is_the_contract(self):
        r = self.client.get("/api/swagger/ghrsst/openapi.json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), json.loads(json.dumps(self.doc)))

    # --- 200 ---
    def test_point_latest(self):
        r, body = self.assert_get(200, lon0=110.0, lat0=12.0)
        self.assertEqual([row["date"] for row in body], ["2025-01-03"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(r.headers["x-store-route"], "daily")

    def test_point_range_fixed_cache_and_field_presence(self):
        r, body = self.assert_get(200, lon0=110.0, lat0=12.0, start="2025-01-02", end="2025-01-01",
                                  append="sst,sst_anomaly")
        self.assertEqual([row["date"] for row in body], ["2025-01-01", "2025-01-02"])  # either order
        self.assertNotIn("sst_anomaly", body[0])          # absent for the day -> OMITTED (OD-10)
        self.assertIsInstance(body[1]["sst_anomaly"], float)
        self.assertEqual(r.headers["cache-control"],
                         f"public, max-age={self.appmod.cfg.LONG_CACHE_SECONDS}")

    def test_point_truncate(self):
        _, body = self.assert_get(200, lon0=110.0, lat0=12.0, mode="TRUNCATE")
        self.assertEqual(body[0]["lon"], round(body[0]["lon"], 5))

    def test_point_coordinates_are_clamped_not_rejected(self):
        _, body = self.assert_get(200, lon0=200.0, lat0=-95.0)          # OD-5: current behaviour
        self.assertAlmostEqual(body[0]["lon"], float(LONS[-1]), places=4)
        self.assertAlmostEqual(body[0]["lat"], float(LATS[0]), places=4)

    def test_only_lon1_is_point_mode(self):
        r, body = self.assert_get(200, lon0=110.0, lat0=12.0, lon1=120.0)   # OD-17
        self.assertIn("x-store-route", r.headers)
        self.assertEqual(len(body), 1)

    def test_bbox_json(self):
        r, body = self.assert_get(200, lon0=110.0, lat0=10.0, lon1=112.0, lat1=12.0,
                                  start="2025-01-01", append="sst,sst_anomaly")
        self.assertTrue(all(row["sst_anomaly"] is None for row in body))   # absent -> null (OD-10)
        self.assertEqual(int(r.headers["x-served-rows"]), len(body))
        self.assertEqual((r.headers["x-stride"], r.headers["x-bbox-format"]), ("1", "json"))
        self.assertIn("x-read-ms", r.headers)
        self.assertNotIn("x-store-route", r.headers)
        keys = [(row["lat"], row["lon"]) for row in body]
        self.assertEqual(keys, sorted(keys))                               # lat asc, then lon asc
        self.assertEqual(r.headers["cache-control"],
                         f"public, max-age={self.appmod.cfg.LONG_CACHE_SECONDS}")

    def test_bbox_without_date_is_latest_and_not_cached(self):
        r, body = self.assert_get(200, lon0=110.0, lat0=10.0, lon1=111.0, lat1=11.0)
        self.assertEqual({row["date"] for row in body}, {"2025-01-03"})
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_bbox_corner_order_does_not_matter(self):
        a = self.get(lon0=112.0, lat0=12.0, lon1=110.0, lat1=10.0, start="2025-01-01")
        b = self.get(lon0=110.0, lat0=10.0, lon1=112.0, lat1=12.0, start="2025-01-01")
        self.assertEqual(json.loads(a.content), json.loads(b.content))

    def test_bbox_antimeridian_is_not_wrapped(self):
        # lon0=170, lon1=-170 is read as -170..170 (both clamped to this grid), not as a wrap.
        _, body = self.assert_get(200, lon0=170.0, lat0=10.0, lon1=-170.0, lat1=10.5,
                                  start="2025-01-01", sample=10)
        self.assertEqual(min(r["lon"] for r in body), float(LONS[0]))

    def test_post_points(self):
        pts = [[100.0, 0.0], [130.0, 30.0], [100.0, 0.0]]
        r, body = self.assert_post(200, {"date": "2025-01-01", "points": pts, "append": "sst,sst_anomaly"})
        self.assertEqual([row["index"] for row in body], [0, 1, 2])
        self.assertEqual(body[0], dict(body[2], index=0))                 # duplicates kept
        self.assertIsNone(body[0]["sst_anomaly"])                         # absent -> null
        self.assertEqual(r.headers["cache-control"], "no-store")

    # --- 400 ---
    def test_get_400_messages(self):
        cases = [
            (dict(lon0=110, lat0=12, start="2025/01/01"), "Invalid date format. Use YYYY-MM-DD."),
            (dict(lon0=110, lat0=12, append="foo"), "Unsupported field(s): foo. Allowed: sst,sst_anomaly,sea_ice"),
            (dict(lon0=110, lat0=12, mode="x"), "Unsupported mode(s): x. Allowed: truncate"),
            (dict(lon0=110, lat0=12, format="coveragejson"), "Unsupported format 'coveragejson'. Allowed: json"),
            (dict(lon0=110, lat0=12, sample=2), "Parameter 'sample' is only supported in BBox mode."),
            (dict(lon0=110, lat0=10, lon1=112, lat1=12, sample=0), "Parameter 'sample' must be >= 1."),
        ]
        for params, detail in cases:
            with self.subTest(params=params):
                _, body = self.assert_get(400, **params)
                self.assertEqual(body["detail"], detail)

    def test_format_is_trimmed_and_case_insensitive(self):
        # Existing handler behaviour (unchanged): `(format or "json").strip().lower()`.
        point = dict(lon0=110.0, lat0=12.0)
        bbox = dict(lon0=110.0, lat0=10.0, lon1=111.0, lat1=11.0, start="2025-01-01")
        for value in ("json", "JSON", " JSON ", "\tJson\n", ""):
            for params in (point, bbox):
                with self.subTest(format=value, bbox="lon1" in params):
                    r, _ = self.assert_get(200, format=value, **params)
                    if "lon1" in params:
                        self.assertEqual(r.headers["x-bbox-format"], "json")
        for value in ("xml", " coveragejson ", "raster", "  "):
            for params in (point, bbox):
                with self.subTest(format=value, bbox="lon1" in params):
                    _, body = self.assert_get(400, format=value, **params)
                    self.assertTrue(body["detail"].startswith("Unsupported format '"), body)

    def test_get_400_other_strings(self):
        for params, needle in [
            (dict(lon0=110, lat0=12, start="2020-01-01", end="2020-01-02"), "Data not exist for requested period"),
            (dict(lon0=110, lat0=10, lon1=112, lat1=12, start="2025-01-01", end="2025-01-02"),
             "BBOX query only allows single-day data"),
            (dict(lon0=110, lat0=10, lon1=112, lat1=12, start="2020-01-01"), "is unavailable"),
        ]:
            with self.subTest(params=params):
                _, body = self.assert_get(400, **params)
                self.assertIn(needle, body["detail"])

    def test_bbox_too_large_is_400(self):
        store = self.appmod.app.state.store
        old = store.bbox_point_limit
        store.bbox_point_limit = 5
        try:
            _, body = self.assert_get(400, lon0=100.0, lat0=0.0, lon1=130.0, lat1=30.0, start="2025-01-01")
        finally:
            store.bbox_point_limit = old
        self.assertTrue(body["detail"].startswith("bbox too large ("), body)

    def test_post_400_messages(self):
        for body_in, detail in [
            ({"date": "20250101", "points": [[110, 12]]}, "Invalid date format. Use YYYY-MM-DD."),
            ({"date": "2025-01-01", "points": []}, "points must be a non-empty list of [lon,lat]."),
            ({"date": "2025-01-01", "points": [[110, 12, 3]]}, "each point must be [lon, lat]."),
            ({"date": "2025-01-01", "points": [[110, 12]], "append": "x"},
             "Unsupported field(s): x. Allowed: sst,sst_anomaly,sea_ice"),
        ]:
            with self.subTest(body=body_in):
                _, body = self.assert_post(400, body_in)
                self.assertEqual(body["detail"], detail)
        _, body = self.assert_post(400, {"date": "2020-01-01", "points": [[110, 12]]})
        self.assertTrue(body["detail"].startswith("day 2020-01-01 not available;"), body)

    # --- 413 ---
    def test_range_too_large(self):
        max_days = self.appmod.cfg.MAX_DAYS
        _, body = self.assert_get(413, lon0=110, lat0=12, start="2023-01-01", end="2025-01-01")
        self.assertEqual(body["detail"]["max_days"], max_days)
        self.assertEqual(body["detail"]["requested_days"], 732)

    def test_too_many_points(self):
        n = self.appmod.cfg.POINTS_BATCH_MAX + 1
        _, body = self.assert_post(413, {"date": "2025-01-01", "points": [[110.0, 12.0]] * n})
        self.assertEqual(body["detail"]["requested_points"], n)

    def test_fan_out_is_413_string(self):
        store = self.appmod.app.state.store
        old = store.batch_chunk_fanout_max
        store.batch_chunk_fanout_max = 1
        try:
            _, body = self.assert_post(413, {"date": "2025-01-01", "points": [[100.0, 0.0], [130.0, 30.0]]})
        finally:
            store.batch_chunk_fanout_max = old
        self.assertEqual(body["detail"], "batch spans 2 chunks > fan-out limit 1; shrink/split the request")

    # --- 422 ---
    def test_422(self):
        _, body = self.assert_get(422, lon0=110.0)
        self.assertEqual(body["detail"][0]["loc"], ["query", "lat0"])
        self.assert_get(422, lon0="east", lat0=1)
        self.assert_get(422, lon0=1, lat0=1, sample="two")
        _, body = self.assert_post(422, {"points": [[110, 12]]})
        self.assertEqual(body["detail"][0]["loc"], ["body", "date"])
        self.assert_post(422, {"date": "2025-01-01", "points": [["a", "b"]]})

    # --- BC-2 (OD-5, option B2-b): non-finite coordinates -> 422 HTTPValidationError ---
    # Baseline 6b32e54: every case below answered 200 with an edge grid cell (one POST case: 500).
    FINITE_MSG = "Input should be a finite number"

    def post_raw(self, raw):
        return self.client.post(POST_PATH, content=raw, headers={"content-type": "application/json"})

    def strict_json(self, r):
        # The body must be standard JSON: no NaN / Infinity tokens anywhere.
        def refuse(token):
            raise AssertionError(f"non-standard JSON token {token} in {r.text[:200]}")
        return json.loads(r.text, parse_constant=refuse)

    def test_bc2_get_non_finite_is_422(self):
        point = dict(lon0=110.0, lat0=12.0)
        bbox = dict(lon0=110.0, lat0=10.0, lon1=111.0, lat1=11.0, start="2025-01-01")
        for name, base in (("lon0", point), ("lat0", point), ("lon1", bbox), ("lat1", bbox)):
            for value in ("nan", "NaN", "inf", "-inf", "Infinity", "-Infinity", "1e400", "-1e400"):
                with self.subTest(param=name, value=value):
                    r, body = self.assert_get(422, **dict(base, **{name: value}))
                    self.strict_json(r)
                    # query input is the raw string as sent: nothing to stringify
                    self.assertEqual(body["detail"], [{"type": "finite_number", "loc": ["query", name],
                                                       "msg": self.FINITE_MSG, "input": value}])

    def test_bc2_post_non_finite_is_422_with_stringified_input(self):
        for token, expected in (("NaN", "NaN"), ("Infinity", "Infinity"), ("-Infinity", "-Infinity"),
                                ("1e400", "Infinity"), ("-1e400", "-Infinity"),
                                ('"nan"', "nan"), ('"inf"', "inf")):         # JSON strings stay as sent
            for j, pair in ((0, f"[{token}, 12]"), (1, f"[110, {token}]")):
                with self.subTest(token=token, position=j):
                    r = self.post_raw('{"date": "2025-01-01", "points": [[110, 12], %s]}' % pair)
                    self.assertEqual(r.status_code, 422, r.text[:200])
                    body = self.conform(r, POST_PATH, "post")
                    self.strict_json(r)
                    self.assertEqual(body["detail"], [{"type": "finite_number", "loc": ["body", "points", 1, j],
                                                       "msg": self.FINITE_MSG, "input": expected}])

    def test_bc2_nested_input_is_json_safe(self):
        # `input` can be the whole body (a missing field) or any non-coordinate field; non-finite
        # floats are stringified at any depth, nothing else changes. The first case was a 500 at
        # baseline (FastAPI echoed the body with a float NaN in it).
        cases = [
            ('{"points": [[NaN, 12], [1, Infinity]]}',
             {"type": "missing", "loc": ["body", "date"], "msg": "Field required",
              "input": {"points": [["NaN", 12], [1, "Infinity"]]}}),
            ('{"date": NaN, "points": [[110, 12]]}',
             {"type": "string_type", "loc": ["body", "date"], "msg": "Input should be a valid string",
              "input": "NaN"}),
            ('{"date": "2025-01-01", "points": -Infinity}',
             {"type": "list_type", "loc": ["body", "points"], "msg": "Input should be a valid list",
              "input": "-Infinity"}),
            ('{"date": "2025-01-01", "points": [[110, 12]], "mode": {"a": [NaN, {"b": Infinity}]}}',
             {"type": "string_type", "loc": ["body", "mode"], "msg": "Input should be a valid string",
              "input": {"a": ["NaN", {"b": "Infinity"}]}}),
        ]
        for raw, first in cases:
            with self.subTest(raw=raw):
                r = self.post_raw(raw)
                self.assertEqual(r.status_code, 422, r.text[:200])
                body = self.conform(r, POST_PATH, "post")
                self.strict_json(r)
                self.assertEqual(body["detail"][0], first)

    def test_bc2_ordinary_422_is_unchanged(self):
        # Byte-exact bodies recorded from baseline 6b32e54 (before BC-2): the handler must defer to
        # FastAPI's default whenever no non-finite float is present.
        for send, expected in [
            (lambda: self.get(lon0=110),
             b'{"detail":[{"type":"missing","loc":["query","lat0"],"msg":"Field required","input":null}]}'),
            (lambda: self.post({"points": [[110, 12]]}),
             b'{"detail":[{"type":"missing","loc":["body","date"],"msg":"Field required",'
             b'"input":{"points":[[110,12]]}}]}'),
            (lambda: self.post({"date": "2025-01-01", "points": "x"}),
             b'{"detail":[{"type":"list_type","loc":["body","points"],"msg":"Input should be a valid list",'
             b'"input":"x"}]}'),
            (lambda: self.post({"date": 5, "points": [[110, 12]]}),
             b'{"detail":[{"type":"string_type","loc":["body","date"],"msg":"Input should be a valid string",'
             b'"input":5}]}'),
        ]:
            r = send()
            self.assertEqual((r.status_code, r.content), (422, expected))
            self.assertEqual(r.headers["content-type"], "application/json")

    def test_bc2_handler_defers_to_fastapi_default(self):
        # Unit level: for errors without non-finite floats the response object comes from FastAPI's
        # own handler (identical bytes); with them, only those floats change.
        import asyncio
        from fastapi.exception_handlers import request_validation_exception_handler
        from fastapi.exceptions import RequestValidationError
        m = self.appmod
        ordinary = RequestValidationError([{"type": "missing", "loc": ("query", "lat0"),
                                            "msg": "Field required", "input": None}])
        mine = asyncio.run(m._validation_handler(None, ordinary))
        default = asyncio.run(request_validation_exception_handler(None, ordinary))
        self.assertEqual((mine.status_code, mine.body), (default.status_code, default.body))
        odd = RequestValidationError([{"type": "x", "loc": ("body", "points", 0, 0), "msg": "m",
                                       "input": [float("nan"), {"k": (float("-inf"), 1.5, "inf", True, 0)}]}])
        out = json.loads(asyncio.run(m._validation_handler(None, odd)).body)
        self.assertEqual(out, {"detail": [{"type": "x", "loc": ["body", "points", 0, 0], "msg": "m",
                                           "input": ["NaN", {"k": ["-Infinity", 1.5, "inf", True, 0]}]}]})

    def test_bc2_finite_out_of_grid_still_clamps(self):
        _, body = self.assert_get(200, lon0=1e300, lat0=-1e300)
        self.assertEqual((body[0]["lon"], body[0]["lat"]), (float(LONS[-1]), float(LATS[0])))
        _, body = self.assert_post(200, {"date": "2025-01-01", "points": [[200.0, -95.0], [-200.0, 95.0]]})
        self.assertEqual([(r["lon"], r["lat"]) for r in body],
                         [(float(LONS[-1]), float(LATS[0])), (float(LONS[0]), float(LATS[-1]))])
        _, body = self.assert_get(200, lon0=-500.0, lat0=-500.0, lon1=500.0, lat1=500.0,
                                  start="2025-01-01", sample=10)
        self.assertEqual(min(r["lon"] for r in body), float(LONS[0]))

    def test_bc2_validation_precedes_handler_checks(self):
        # Framework validation runs before the handler's own 400 checks, exactly as for a non-numeric
        # coordinate today: a request that is ALSO wrong elsewhere is a 422 for its non-finite value.
        r, _ = self.assert_get(422, lon0="east", lat0=12, append="foo")     # baseline precedence
        r, _ = self.assert_get(422, lon0="nan", lat0=12, append="foo")      # same rule for NaN
        r = self.post_raw('{"date": "bad", "points": [[NaN, 12]]}')
        self.assertEqual(r.status_code, 422)
        r = self.post_raw('{"date": "2025-01-01", "points": [[NaN]]}')        # wrong length too
        self.assertEqual(r.status_code, 422)
        # only lon1 (point mode, lon1 otherwise unused) is still validated
        self.assert_get(422, lon0=110.0, lat0=12.0, lon1="nan")

    # --- 503 overload ---
    def test_overload_503_has_retry_after(self):
        app = self.appmod.app
        real = app.state.bex
        app.state.bex = self.appmod.BoundedExecutor(workers=1, queue_max=-1, wait_ms=10)  # 0 permits
        try:
            for r, path, method in [
                (self.get(lon0=110.0, lat0=12.0), GET_PATH, "get"),
                (self.get(lon0=110.0, lat0=10.0, lon1=112.0, lat1=12.0, start="2025-01-01"), GET_PATH, "get"),
                (self.post({"date": "2025-01-01", "points": [[110.0, 12.0]]}), POST_PATH, "post"),
            ]:
                with self.subTest(path=path, url=str(r.url)):
                    self.assertEqual(r.status_code, 503)
                    self.assertEqual(self.conform(r, path, method), {"detail": "server overloaded; retry later"})
                    self.assertEqual(r.headers["retry-after"], "1")
                    self.assertNotIn("cache-control", r.headers)
        finally:
            app.state.bex.shutdown()
            app.state.bex = real

    # --- BC-1 (OD-4): a well-formed but non-existent day is a 400, on every path ---
    # Baseline 6b32e54: GET point/range -> 500 text/plain; bbox / POST -> 400 with a different body.
    BAD_DAYS = ("2025-02-30", "2025-13-01", "0000-01-01", "2025-01-01\n")
    INVALID_DATE = {"detail": "Invalid date format. Use YYYY-MM-DD."}

    def test_bc1_non_existent_day_is_400_on_every_path(self):
        for day in self.BAD_DAYS + ("2025/01/01", "25-01-01"):          # + malformed (unchanged)
            for label, params in [
                ("point", dict(lon0=110.0, lat0=12.0, start=day)),
                ("range start", dict(lon0=110.0, lat0=12.0, start=day, end="2025-01-02")),
                ("range end", dict(lon0=110.0, lat0=12.0, start="2025-01-01", end=day)),
                ("bbox", dict(lon0=110.0, lat0=10.0, lon1=111.0, lat1=11.0, start=day)),
            ]:
                with self.subTest(day=day, path=label):
                    _, body = self.assert_get(400, **params)
                    self.assertEqual(body, self.INVALID_DATE)
            with self.subTest(day=day, path="POST"):
                _, body = self.assert_post(400, {"date": day, "points": [[110.0, 12.0]]})
                self.assertEqual(body, self.INVALID_DATE)

    def test_bc1_valid_days_unchanged(self):
        self.assert_get(200, lon0=110.0, lat0=12.0, start="2025-01-01")
        self.assert_get(200, lon0=110.0, lat0=12.0, start="2024-02-29", end="2025-01-03")   # leap day
        self.assert_post(200, {"date": "2025-01-02", "points": [[110.0, 12.0]]})

    def test_trailing_newline_date_is_400_and_off_schema(self):
        # The generated schema and the handler agree: "2025-01-01\n" is invalid in both.
        day = "2025-01-01\n"
        params = {p["name"]: p for p in self.doc["paths"][GET_PATH]["get"]["parameters"]}
        self.assertNotEqual(schema_errors(self.doc, params["start"]["schema"], day), [])
        self.assertNotEqual(schema_errors(self.doc, {"$ref": "#/components/schemas/Date"}, day), [])
        _, body = self.assert_get(400, lon0=110.0, lat0=12.0, start=day)
        self.assertEqual(body, self.INVALID_DATE)
        _, body = self.assert_post(400, {"date": day, "points": [[110.0, 12.0]]})
        self.assertEqual(body, self.INVALID_DATE)

    def test_no_500_is_documented(self):
        for path, method in ((GET_PATH, "get"), (POST_PATH, "post")):
            self.assertNotIn("500", self.doc["paths"][path][method]["responses"])

    def test_errors_carry_no_cache_control(self):
        r = self.get(lon0=110, lat0=12, append="foo")
        self.assertNotIn("cache-control", r.headers)


# ---- 503 without Retry-After: nothing to serve ---------------------------------------------
@NEEDS_JSONSCHEMA
class EmptyStoreTests(ContractAssertions):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghrsst_v1_empty_")
        cls.ctx = _ConfiguredApp(env={"GHRSST_ZARR_PATH": cls.tmp})
        cls.appmod = cls.ctx.__enter__()
        from fastapi.testclient import TestClient
        cls.cm = TestClient(cls.appmod.app, raise_server_exceptions=False)
        cls.client = cls.cm.__enter__()
        cls.doc = cls.appmod.app.openapi()

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)
        cls.ctx.__exit__(None, None, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_available_dates_503s_have_no_retry_after(self):
        for params, detail in [
            (dict(lon0=110.0, lat0=12.0), "No available dates."),
            (dict(lon0=110.0, lat0=10.0, lon1=112.0, lat1=12.0), "No available dates for spatial queries."),
        ]:
            with self.subTest(params=params):
                r = self.client.get(GET_PATH, params=params)
                self.assertEqual(r.status_code, 503)
                self.assertEqual(self.conform(r, GET_PATH, "get"), {"detail": detail})
                self.assertNotIn("retry-after", r.headers)

    def test_post_with_no_data_is_400_not_503(self):
        r = self.client.post(POST_PATH, json={"date": "2025-01-01", "points": [[110.0, 12.0]]})
        self.assertEqual(r.status_code, 400)
        self.conform(r, POST_PATH, "post")


# ---- spatial window (production-like tiered store, enforcement on) --------------------------
SW_DAYS = [(datetime.date(2026, 6, 20) + datetime.timedelta(days=i)).isoformat() for i in range(7)]
# daily: all 7 days; base cube: days 0-3; delta (the spatial window): days 4-5;
# day 6 is in daily only -> the date-less bbox picks it and must NOT fall back to day 5.


def build_tiered(tmp):
    from ingest.build_timecube_bulk import build_timecube_bulk
    from ingest.dual_write import append_to_delta
    daily = os.path.join(tmp, "daily")
    lon = np.linspace(120, 130, NX).astype(np.float32)
    lat = np.linspace(10, 20, NY).astype(np.float32)
    for d in SW_DAYS:
        build_daily_day(daily, d, variables=("sst", "sst_anomaly", "sea_ice"), lon=lon, lat=lat)
    base = os.path.join(tmp, "base")
    build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=90, shard_spatial=16, read_block=16,
                        workers=2, end_day=SW_DAYS[3])
    delta = os.path.join(tmp, "delta")
    for d in SW_DAYS[4:6]:
        append_to_delta(daily, delta, d, spatial_chunk=16, shard_spatial=16)
    return {"GHRSST_ZARR_PATH": daily, "GHRSST_TIMECUBE_PATH": base, "GHRSST_DELTACUBE_PATH": delta}


@NEEDS_JSONSCHEMA
class SpatialWindowTests(ContractAssertions):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghrsst_v1_sw_")
        cls.ctx = _ConfiguredApp(env=build_tiered(cls.tmp), cfg={"SPATIAL_WINDOW_ENFORCE": 1})
        cls.appmod = cls.ctx.__enter__()
        from fastapi.testclient import TestClient
        cls.cm = TestClient(cls.appmod.app, raise_server_exceptions=False)
        cls.client = cls.cm.__enter__()
        cls.doc = cls.appmod.app.openapi()

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)
        cls.ctx.__exit__(None, None, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def bbox(self, **extra):
        return self.client.get(GET_PATH, params=dict(lon0=121, lat0=11, lon1=122, lat1=12, **extra))

    def assert_window_error(self, r, path, method, day):
        self.assertEqual(r.status_code, 400, r.text[:300])
        detail = self.conform(r, path, method)["detail"]
        from store import spatial_policy
        self.assertEqual(detail["requested_day"], day)
        self.assertEqual(detail["available_spatial_window"], f"{SW_DAYS[4]}..{SW_DAYS[5]}")
        # retention TARGET, not the 2-day window actually available (OD-7)
        self.assertEqual(detail["spatial_window_days"], spatial_policy.SPATIAL_WINDOW_DAYS)
        self.assertNotEqual(detail["spatial_window_days"], 2)

    def test_bbox_in_window(self):
        r = self.bbox(start=SW_DAYS[5])
        self.assertEqual(r.status_code, 200)
        self.conform(r, GET_PATH, "get")

    def test_bbox_outside_window(self):
        self.assert_window_error(self.bbox(start=SW_DAYS[1]), GET_PATH, "get", SW_DAYS[1])

    def test_post_outside_window(self):
        r = self.client.post(POST_PATH, json={"date": SW_DAYS[1], "points": [[125.0, 15.0]]})
        self.assert_window_error(r, POST_PATH, "post", SW_DAYS[1])

    def test_bc1_non_existent_day_is_a_date_error_not_a_window_error(self):
        # BC-1 body change on the spatial paths: baseline 6b32e54 answered 2025-02-30 with the
        # SpatialWindowError object (the day is not in the delta); it is now the date-format string.
        # Status stays 400.
        expected = {"detail": "Invalid date format. Use YYYY-MM-DD."}
        r = self.bbox(start="2025-02-30")
        self.assertEqual((r.status_code, self.conform(r, GET_PATH, "get")), (400, expected))
        r = self.client.post(POST_PATH, json={"date": "2025-02-30", "points": [[125.0, 15.0]]})
        self.assertEqual((r.status_code, self.conform(r, POST_PATH, "post")), (400, expected))

    def test_post_in_window(self):
        r = self.client.post(POST_PATH, json={"date": SW_DAYS[4], "points": [[125.0, 15.0]],
                                              "append": "sst,sst_anomaly,sea_ice"})
        self.assertEqual(r.status_code, 200)
        self.conform(r, POST_PATH, "post")

    def test_bbox_without_date_uses_daily_latest_without_fallback(self):
        # Daily latest (day 6) is not in the delta: 400, not a silent fallback to day 5.
        self.assert_window_error(self.bbox(), GET_PATH, "get", SW_DAYS[6])

    def test_point_history_is_not_gated(self):
        r = self.client.get(GET_PATH, params=dict(lon0=125, lat0=15, start=SW_DAYS[0], end=SW_DAYS[6],
                                                  append="sst,sst_anomaly"))
        self.assertEqual(r.status_code, 200)
        body = self.conform(r, GET_PATH, "get")
        self.assertEqual([row["date"] for row in body], SW_DAYS)
        self.assertIn(r.headers["x-store-route"], ("cube", "daily", "mixed"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
