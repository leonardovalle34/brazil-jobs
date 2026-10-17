"""
Tests for workflows/caged_monthly.yaml.

Cloud Workflows has no local emulator, so this file carries a minimal
interpreter for exactly the subset of the syntax the workflow uses
(assign, switch, call, try/except, return, raise, next, subworkflows,
${...} expressions) and runs the REAL YAML against a fake pipeline
service. That covers our control flow — the loop, the stop conditions,
the 404 handling, the 12-month limit. It does NOT prove Google's parser
accepts the file: deploying it (gcloud workflows deploy) is still the
final syntax check.

Like the real runtime, http.* calls raise on a non-2xx response, which
is exactly why the workflow needs try/except around /download.
"""

import json
import os
import re
from types import SimpleNamespace

import pytest
import yaml

WORKFLOW_PATH = os.path.join(os.path.dirname(__file__), "..", "workflows", "caged_monthly.yaml")


# --- minimal Cloud Workflows interpreter --------------------------------

class AttrDict(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise KeyError(f"key {name!r} not found") from None


def wrap(value):
    if isinstance(value, dict):
        return AttrDict({k: wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [wrap(v) for v in value]
    return value


class WorkflowError(Exception):
    def __init__(self, payload):
        super().__init__(payload)
        self.payload = payload


class Returned(Exception):
    def __init__(self, value):
        self.value = value


EXPR_FUNCTIONS = {
    "len": len,
    "string": str,
    "default": lambda value, fallback: fallback if value is None else value,
    "map": SimpleNamespace(get=lambda m, key: m.get(key) if isinstance(m, dict) else None),
    "list": SimpleNamespace(concat=lambda items, value: list(items) + [value]),
    "json": SimpleNamespace(encode_to_string=json.dumps),
}


def evaluate(value, variables):
    if isinstance(value, dict):
        return AttrDict({k: evaluate(v, variables) for k, v in value.items()})
    if isinstance(value, list):
        return [evaluate(v, variables) for v in value]
    if not isinstance(value, str) or "${" not in value:
        return value
    whole = re.fullmatch(r"\$\{(.*)\}", value, re.S)
    if whole and whole.group(1).count("${") == 0 and "}/" not in value:
        return eval(whole.group(1), dict(EXPR_FUNCTIONS), dict(variables))
    # "${service_url}/download" style interpolation
    return re.sub(r"\$\{([^}]*)\}", lambda m: str(eval(m.group(1), dict(EXPR_FUNCTIONS), dict(variables))), value)


class Interpreter:
    def __init__(self, definition, http):
        self.definition = definition
        self.http = http
        self.logs = []

    def run(self, argument):
        return self.call_workflow("main", [argument])

    def call_workflow(self, name, args):
        sub = self.definition[name]
        variables = dict(zip(sub.get("params", []), [wrap(a) for a in args]))
        try:
            self.run_steps(sub["steps"], variables)
        except Returned as r:
            return r.value
        return None

    def run_steps(self, steps, variables):
        names = [next(iter(s)) for s in steps]
        i = 0
        while i < len(steps):
            name = names[i]
            jump = self.run_step(steps[i][name], variables)
            if jump == "end":
                return
            if jump is not None:
                if jump not in names:
                    return jump  # let an enclosing step list resolve it
                i = names.index(jump)
            else:
                i += 1

    def run_step(self, step, variables):
        if "try" in step:
            try:
                self.run_step(step["try"], variables)
            except WorkflowError as e:
                variables[step["except"]["as"]] = wrap(e.payload)
                self.run_steps(step["except"]["steps"], variables)
            return step.get("next")
        if "assign" in step:
            for item in step["assign"]:
                for k, v in item.items():
                    variables[k] = evaluate(v, variables)
        if "call" in step:
            result = self.do_call(step["call"], evaluate(step.get("args", {}), variables))
            if "result" in step:
                variables[step["result"]] = result
        if "switch" in step:
            for cond in step["switch"]:
                if evaluate(cond["condition"], variables):
                    return self.run_branch(cond, variables)
        if "steps" in step:
            jump = self.run_steps(step["steps"], variables)
            if jump:
                return jump
        if "raise" in step:
            raise WorkflowError(evaluate(step["raise"], variables))
        if "return" in step:
            raise Returned(evaluate(step["return"], variables))
        return step.get("next")

    def run_branch(self, branch, variables):
        body = {k: v for k, v in branch.items() if k != "condition"}
        return self.run_step(body, variables)

    def do_call(self, target, args):
        if target == "sys.log":
            self.logs.append((args.get("severity"), args.get("text")))
            return None
        if target in ("http.get", "http.post"):
            code, body = self.http(target.split(".")[1].upper(), args["url"], args.get("body"))
            if code >= 400:
                raise WorkflowError({"code": code, "body": body, "message": f"HTTP {code}"})
            return wrap({"code": code, "body": body})
        return self.call_workflow(target, [args[p] for p in self.definition[target]["params"]])


# --- fake pipeline service ----------------------------------------------

class FakeService:
    """Mimics app.py: next-period answers from `published - loaded`."""

    def __init__(self, loaded=(), published=(), unpublished_on_download=()):
        self.loaded = set(loaded)
        self.published = set(published)
        self.unpublished_on_download = set(unpublished_on_download)
        self.calls = []

    def __call__(self, method, url, body):
        path = url.rsplit("/", 1)[1]
        self.calls.append((path, (body or {}).get("period")))
        if path == "next-period":
            pending = sorted(self.published - self.loaded)
            if not pending:
                return 200, {"status": "no_file", "period": None,
                             "message": "Nenhum mês novo publicado no CAGED"}
            return 200, {"status": "ok", "period": pending[0]}
        period = body["period"]
        if path == "download":
            if period not in self.published or period in self.unpublished_on_download:
                return 404, {"status": "no_file", "period": period,
                             "message": f"Arquivo do CAGED para {period} ainda não foi publicado"}
            return 200, {"status": "ok", "raw_path": f"gs://b/raw_{period}.parquet"}
        if path == "transform":
            return 200, {"status": "ok", "fact_path": f"gs://b/fact_{period}.parquet"}
        if path == "load":
            if period in self.loaded:
                return 409, {"status": "already_loaded", "period": period}
            self.loaded.add(period)
            return 200, {"status": "ok", "rows_loaded": 1000}
        raise AssertionError(f"unexpected call {url}")


@pytest.fixture
def definition():
    with open(WORKFLOW_PATH) as f:
        return yaml.safe_load(f)


def run(definition, service, argument=None):
    interpreter = Interpreter(definition, service)
    return interpreter.run(argument if argument is not None else {}), interpreter


def periods(*ps):
    return set(ps)


# --- tests ----------------------------------------------------------------

def test_no_new_month_succeeds_with_no_file_result(definition):
    service = FakeService(loaded=periods("202608"), published=periods("202608"))

    result, interpreter = run(definition, service)

    assert result == {"status": "no_file", "message": "Nenhum mês novo publicado no CAGED", "loaded": []}
    assert service.calls == [("next-period", None)]  # nothing downloaded
    assert ("INFO", "Nenhum mês novo publicado no CAGED") in interpreter.logs


def test_loop_backfills_every_missing_month_in_order_then_stops(definition):
    service = FakeService(
        loaded=periods("202509", "202512"),
        published=periods("202509", "202510", "202511", "202512", "202601"),
    )

    result, _ = run(definition, service)

    assert result == {"status": "ok", "loaded": ["202510", "202511", "202601"]}
    steps = [path for path, _ in service.calls]
    assert steps == ["next-period", "download", "transform", "load"] * 3 + ["next-period"]


def test_loop_stops_at_12_months_per_execution(definition):
    published = {f"2025{m:02d}" for m in range(1, 13)} | {f"2026{m:02d}" for m in range(1, 13)}
    service = FakeService(published=published)

    result, interpreter = run(definition, service)

    assert result["status"] == "ok"
    assert len(result["loaded"]) == 12
    assert result["loaded"][0] == "202501" and result["loaded"][-1] == "202512"
    assert any("Limite de 12 meses" in text for _, text in interpreter.logs)


def test_download_404_during_loop_ends_successfully_with_what_was_loaded(definition):
    service = FakeService(
        published=periods("202510", "202511"),
        unpublished_on_download=periods("202511"),
    )

    result, _ = run(definition, service)

    assert result == {"status": "ok", "loaded": ["202510"]}


def test_manual_override_processes_only_that_period(definition):
    service = FakeService(published=periods("202508", "202509"))

    result, _ = run(definition, service, {"period": "202508"})

    assert result == {"status": "ok", "loaded": ["202508"], "rows_loaded": 1000}
    assert ("next-period", None) not in service.calls


def test_manual_override_of_unpublished_period_succeeds_as_no_file(definition):
    service = FakeService(published=())

    result, _ = run(definition, service, {"period": "202610"})

    assert result == {
        "status": "no_file",
        "period": "202610",
        "message": "Arquivo do CAGED para 202610 ainda não foi publicado",
        "loaded": [],
    }


def test_real_errors_still_fail_the_execution(definition):
    service = FakeService(loaded=periods("202508"), published=periods("202508"))

    with pytest.raises(WorkflowError) as exc_info:
        run(definition, service, {"period": "202508"})  # /load answers 409
    assert exc_info.value.payload["code"] == 409


def test_previous_month_computation_is_gone(definition):
    step_names = [next(iter(s)) for s in definition["main"]["steps"]]
    assert "compute_reference_period" not in step_names
    assert "sys.now" not in open(WORKFLOW_PATH).read()
