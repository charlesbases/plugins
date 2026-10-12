"""Full frozen publisher-domain traversal through public news operations.

The bounded shard is an engineering unit. It never becomes the publisher or
article denominator. A stopped crawler reports every remaining publisher and
known original URL; an unknown global news denominator stays unknown.
"""
import copy
import hashlib
import math
import re
import urllib.parse

from artifacts import Artifacts
from contracts import fields, fingerprint, instant, require
from file_io import read_artifact_json, write_artifact_json
from state_store import Store
from workflow_discovery import _lock

MAX_PRODUCER_ATTEMPTS = 9
FOLLOWUP_SCHEMA = "source_derived_news_tasks_v1"
FOLLOWUP_TASK_SCHEMA = "source_derived_news_task_v1"
FOLLOWUP_BUDGET_SCOPE = "all_initial_and_source_derived_actual_source_attempts_including_failures_and_producer_retries"


def _parameters(parameters):
    value = {} if parameters is None else copy.deepcopy(parameters)
    fields(value, set(), {"timeout_seconds", "budget_seconds", "max_urls_per_source", "max_pages_per_source", "max_total_source_requests", "max_total_bytes", "max_total_elapsed_seconds"}, "news workflow engineering parameters")
    out = {"timeout_seconds": 8, "budget_seconds": 60, "max_urls_per_source": 20, "max_pages_per_source": 10, **value}
    require(type(out["max_urls_per_source"]) is int and 2 <= out["max_urls_per_source"] <= 20, "Atomic body/PDF shard requires a 2-20 URL batch budget")
    require(type(out["max_pages_per_source"]) is int and 1 <= out["max_pages_per_source"] <= 10, "Source shard page budget must be 1-10")
    require(type(out["timeout_seconds"]) in {int, float} and 0 < out["timeout_seconds"] <= 30 and type(out["budget_seconds"]) in {int, float} and 0 < out["budget_seconds"] <= 60, "News shard clock budgets are outside the original controller limits")
    for key in ("max_total_source_requests", "max_total_bytes", "max_total_elapsed_seconds"):
        if key in out:
            require(type(out[key]) is int and 0 < out[key] <= 10**12, "Positive finite cumulative news budget required")
    return out


def _accepted(store, artifacts, request_id):
    operation = store.operation(request_id)
    require(operation and operation["status"] == "completed" and operation["request"]["operation"] == "news_collect", "News shard is not a completed public operation")
    import stage_validation
    bindings = artifacts.read_json(operation["result"]["automatic_validation"]["operation_bindings_ref"])
    stage_validation.validate_operation_result(operation["request"], operation["result"], store, artifacts, bindings)
    return artifacts.read_json(operation["result"]["manifest_ref"])


def _cost(store, artifacts, request):
    """Every original producer attempt spends the same parent budget."""
    counters = {"requests": 0, "bytes": 0, "elapsed_seconds": 0.0, "producer_attempts": 0}
    require(request["operation"] == "news_collect", "News costs require the original news request")
    operation_id, payload = request["request_id"], request["payload"]
    stage_key = fingerprint({"operation_id": operation_id, "stage": "news"})
    attempts = []
    # StageRuntime reserves exactly three rounds of three attempt keys. Read
    # those original rows directly instead of scanning every prior operation
    # in the plan on every shard. Missing consumed evidence is an error, not
    # a zero charge; interrupted producers retain their complete reservation.
    for round_no in range(1, 4):
        key = stage_key+":r"+str(round_no)
        budget = store.get("validation_budget", key)
        used = 0
        if budget is not None:
            fields(budget, {"used"}, label="original news attempt budget")
            require(type(budget["used"]) is int and 0 <= budget["used"] <= 3,
                    "Original news attempt budget is invalid")
            binding = store.get("validation_binding", key)
            frozen = {"payload": payload, "operation_id": operation_id}
            require(binding is not None and binding["operation_id"] == operation_id
                    and binding["stage"] == "news" and type(binding["round_no"]) is int
                    and binding["round_no"] == round_no and type(binding["max_attempts"]) is int
                    and 1 <= binding["max_attempts"] <= 3 and budget["used"] <= binding["max_attempts"]
                    and binding["inputs_hash"] == fingerprint(frozen)
                    and artifacts.read_json(binding["inputs_ref"]) == frozen,
                    "Original news budget lacks its source-bound stage inputs")
            used = budget["used"]
        for number in range(1, 4):
            namespace = (operation_id if round_no == 1 and number == 1 else
                         operation_id+":validation:"+stage_key[:16]+":r"+str(round_no)+":a"+str(number))
            attempt = store.get("validation_attempt_start", key+":a"+str(number))
            journal = store.get("news-operation", fingerprint({"operation_id": namespace}))
            if number > used:
                require(attempt is None and journal is None,
                        "News attempt or producer journal is outside its original consumed budget")
                continue
            require(attempt is not None, "Consumed news attempt is missing its original start")
            fields(attempt, {"number", "namespace", "operation_id", "round_no", "stage", "binding_key", "started_at"},
                   label="original news attempt start")
            require(type(attempt["number"]) is int and attempt["number"] == number
                    and type(attempt["round_no"]) is int and attempt["round_no"] == round_no
                    and attempt["operation_id"] == operation_id and attempt["stage"] == "news"
                    and attempt["binding_key"] == key and attempt["namespace"] == namespace,
                    "Original news attempt identity or namespace changed")
            instant(attempt["started_at"])
            attempts.append((attempt, journal))
    for attempt, journal in attempts:
        counters["producer_attempts"] += 1
        if not journal:
            counters["requests"] += payload["max_urls_per_source"]
            counters["bytes"] += payload["max_urls_per_source"]*2*1024*1024
            counters["elapsed_seconds"] += payload["budget_seconds"]
            continue
        value = artifacts.read_json(journal["manifest_ref"])
        require(value["operation_id"] == value["collection_id"] == attempt["namespace"]
                and value["run_id"] == payload["run_id"] and value["cutoff_at"] == payload["cutoff_at"]
                and value["window_start"] == payload["window_start"]
                and journal["request_hash"] == value["request_hash"]
                and value["budget_seconds"] == payload["budget_seconds"],
                "Original news cost manifest belongs to another producer or request")
        require(type(value["retrievals"]) is list
                and len({row["retrieval_id"] for row in value["retrievals"]}) == len(value["retrievals"]),
                "Original news cost retrievals are duplicated or invalid")
        for row in value["retrievals"]:
            require(row["retrieval_id"] == fingerprint({"operation_id": attempt["namespace"],
                    "source_id": row["registry_source_id"], "url": row["requested_url"]})
                    and store.get("news-retrieval", row["retrieval_id"]) == row,
                    "News cost retrieval lacks its original producer journal")
            require(("bytes" not in row and row["state"] == "unavailable")
                    or (type(row.get("bytes")) is int and 0 <= row["bytes"] <= 2*1024*1024),
                    "Original news byte charge is invalid")
        counters["requests"] += len(value["retrievals"])
        counters["bytes"] += sum(row.get("bytes", 2*1024*1024) for row in value["retrievals"])
        # Existing low-level receipts publish their declared clock budget;
        # charge that full allowance conservatively instead of guessing time.
        counters["elapsed_seconds"] += value["budget_seconds"]
    return counters


def _receipt(journal, collection):
    latest = {row["source_id"]: row for row in collection["source_results"]}
    pending = {source: sorted(set(latest[source]["discovery_pending_urls"])
        | set(latest[source]["pending_urls"])
        | set(latest[source].get("selected_seed_not_attempted_urls", []))) for source in journal["sources"]}
    return {"collection": collection, "controller": {"schema_id": "frozen_publisher_scope_news_controller_v1",
        "status": journal["status"], "workflow_id": journal["workflow_id"], "global_resource_plan": journal["resource_plan"],
        "consumed": journal["consumed"], "reason": journal.get("reason"),
        "frozen_source_ids": journal["sources"], "pending_source_ids": sorted(source for source in journal["sources"] if pending[source] or not latest[source]["advance_cursor"]),
        "pending_urls_by_source": pending,
        "world_news_denominator": None, "source_time_window_article_denominator": None,
        "all_news_coverage_claimed": False, "news_time_window_exhaustiveness_proven": False,
        "scope_gap": "authority_directory_institutions_do_not_enumerate_all_company_market_local_or_international_news",
        "trade_ready": False}}


def _selected_tasks(original, params, gkg):
    tasks = []
    batches = original["selection_batches"]
    for batch in [row for row in batches if row["source_id"] not in gkg]+[row for row in batches if row["source_id"] in gkg]:
        for offset in range(0, len(batch["urls"]), params["max_urls_per_source"]):
            tasks.append({"source_id": batch["source_id"], "urls": batch["urls"][offset:offset+params["max_urls_per_source"]]})
    return tasks


def _limits(original, params):
    resource = original["resource_plan"]
    capacity = {"requests": resource["max_source_requests"], "bytes": resource["max_total_bytes"],
                "elapsed_seconds": resource["max_total_elapsed_seconds"]}
    for parameter, key in (("max_total_source_requests", "requests"), ("max_total_bytes", "bytes"), ("max_total_elapsed_seconds", "elapsed_seconds")):
        if parameter in params:
            capacity[key] = min(capacity[key], params[parameter])
    return capacity, {"GKG": resource["max_gkg_source_requests"], "official": resource["max_official_source_requests"]}


def _child_reference(store, artifacts, identity, *, result_ref=None):
    manifest = _accepted(store, artifacts, identity)
    operation = store.operation(identity)
    receipt = store.get("operation_result", identity)
    require(receipt == operation["result"], "News child lacks its original public receipt")
    # Producers may give the existing public result a content address.
    # Validators receive that exact reference and never create an artifact.
    reference = artifacts.put_json(receipt) if result_ref is None else result_ref
    require(artifacts.read_json(reference) == operation["result"], "News child receipt differs from its public operation")
    return {"public_operation_id": identity, "result_ref": reference,
            "manifest_ref": operation["result"]["manifest_ref"]}, manifest


def _reference_child(row, store, artifacts, run, window_start):
    fields(row, {"public_operation_id", "result_ref", "manifest_ref"}, label="original source child reference")
    operation = store.operation(row["public_operation_id"])
    require(operation is not None and operation["status"] == "completed"
            and operation["request"]["operation"] == "news_collect"
            and artifacts.read_json(row["result_ref"]) == operation["result"]
            and operation["result"]["manifest_ref"] == row["manifest_ref"],
            "Derived source parent lacks its exact accepted public operation")
    manifest = _accepted(store, artifacts, row["public_operation_id"])
    require(manifest == artifacts.read_json(row["manifest_ref"])
            and manifest["run_id"] == run["run_id"] and manifest["window_start"] == window_start
            and manifest["cutoff_at"] == run["publish_cutoff"], "Derived source parent changed its original window")
    return manifest


def _costs(identities, store, artifacts, gkg, *, memo=None):
    consumed = {"requests": 0, "bytes": 0, "elapsed_seconds": 0.0, "producer_attempts": 0}
    classes = {"GKG": 0, "official": 0}
    for identity in identities:
        operation = store.operation(identity)
        require(operation is not None and operation["request"]["operation"] == "news_collect",
                "Cumulative source cost omitted its original public operation")
        request = operation["request"]
        if memo is not None and identity in memo:
            value = memo[identity]
        else:
            value = _cost(store, artifacts, request)
            if memo is not None:
                memo[identity] = value
        for key, amount in value.items():
            consumed[key] += amount
        sources = request["payload"].get("sources")
        require(type(sources) is list and len(sources) == 1, "Derived source costs require an exact single-source request")
        classes["GKG" if sources[0]["source_id"] in gkg else "official"] += value["requests"]
    return consumed, classes


def _link_domain(children, original, run, registry, artifacts, store, *, memo=None):
    """Reparse actual originals; a mutable pending URL is never an authority."""
    import news
    import source_fetch
    edges, captures, listings = {}, {}, {}
    if memo is not None:
        # Invocation-local graph construction: each actual parent is reparsed
        # once in this independent validation call. Nothing is loaded from a
        # prior invocation or a persisted acceptance boolean.
        for child, manifest in children:
            identity = (child["public_operation_id"], child["manifest_ref"]["sha256"])
            if identity not in memo:
                memo[identity] = _link_domain([(child, manifest)], original, run, registry, artifacts, store)[:3]
            child_edges, child_captures, child_listings = memo[identity]
            for key, values in child_edges.items():
                edges.setdefault(key, []).extend(edge for edge in values if edge not in edges.get(key, []))
            for key, values in child_captures.items():
                captures.setdefault(key, []).extend(values)
            for source, values in child_listings.items():
                listings.setdefault(source, set()).update(values)
        return edges, captures, listings, _graph_pending(edges, captures, original, registry)
    for child, manifest in children:
        parsed_parents = {}
        for row in manifest["retrievals"]:
            source = row["registry_source_id"]
            key = (source, row["requested_url"])
            captures.setdefault(key, []).append(row)
            if row.get("raw_ref") is None:
                continue
            rule = registry["sources"][source]
            require(store.get("news-retrieval", row["retrieval_id"]) == row,
                    "Derived link parent differs from its original capture journal")
            source_fetch.validate_capture_provenance(row, registry)
            raw = artifacts.read(row["raw_ref"])
            require(len(raw) == row["bytes"] and hashlib.sha256(raw).hexdigest() == row["raw_sha256"],
                    "Derived link parent bytes changed")
            text, encoding = source_fetch.decode(raw, row["content_type"], row["encoding"], rule)
            require(encoding == row["encoding"] and (hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None) == row["text_sha256"],
                    "Derived link parent decoding changed")
            parsed = news.parse_capture({**row, "raw_bytes": raw, "text": text}, rule,
                linked_parent=parsed_parents.get(row.get("linked_parent_retrieval_id")))
            require(parsed["state"] == row["state"], "Derived parent classification differs from actual source")
            parsed_parents[row["retrieval_id"]] = {key: value for key, value in parsed.items() if key != "body"}
            if parsed["state"] == "lead":
                listings.setdefault(source, set()).add(row["requested_url"])
            if parsed["state"] not in {"lead", "body"} or rule.get("max_follow_links", 5) == 0:
                continue
            next_pages = parsed.get("listing_links", []) if parsed["state"] == "lead" else []
            for link in parsed.get("links", [])+next_pages:
                target = urllib.parse.urljoin(row["final_url"], link).split("#", 1)[0]
                try:
                    target = source_fetch.checked_url(target, rule)
                except ValueError:
                    continue
                path = urllib.parse.urlsplit(target).path
                pdf = bool(re.search(r"\.pdf$", path, re.I)) and bool(rule.get("pdf_attachments"))
                article = bool(re.search(rule.get("article_pattern", r"$^"), path))
                listing = link in next_pages or target in next_pages or bool(re.search(rule.get("listing_pattern", r"$^"), path))
                if parsed["state"] == "body" and not pdf:
                    continue
                if not (article or listing or pdf):
                    continue
                kind = "pdf" if pdf and parsed["state"] == "body" else "article" if article else "listing"
                parent = {"public_operation_id": child["public_operation_id"], "manifest_ref": child["manifest_ref"],
                    "retrieval_id": row["retrieval_id"], "raw_ref": row["raw_ref"], "final_url": row["final_url"]}
                edge = {"source_id": source, "url": target, "kind": kind, "parent_capture": parent}
                choices = edges.setdefault((source, target), [])
                if edge not in choices:
                    choices.append(edge)
    return edges, captures, listings, _graph_pending(edges, captures, original, registry)


def _graph_pending(edges, captures, original, registry):
    import source_fetch
    pending = {}
    for batch in original["selection_batches"]:
        for url in batch["urls"]:
            source = batch["source_id"]
            checked = source_fetch.checked_url(url, registry["sources"][source])
            if (source, checked) not in captures:
                pending[(source, checked)] = {"source_id": source, "url": checked, "parent_captures": [],
                    "reason": "initial_selected_URL_not_attempted"}
    for key, choices in edges.items():
        if key not in captures:
            pending[key] = {"source_id": key[0], "url": key[1],
                "parent_captures": [edge["parent_capture"] for edge in choices], "reason": "original_source_link_not_acquired"}
    for key, rows in captures.items():
        if not any(row["state"] in {"body", "lead"} for row in rows):
            pending[key] = {"source_id": key[0], "url": key[1],
                "parent_captures": [edge["parent_capture"] for edge in edges.get(key, [])],
                "reason": "source_capture_unavailable_or_partial"}
    return [pending[key] for key in sorted(pending)]


def _followup_request(identity, run, window_start, source, urls, params):
    return {"schema_version": 4, "request_id": identity, "operation": "news_collect", "payload": {
        "run_id": run["run_id"], "cutoff_at": run["publish_cutoff"], "window_start": window_start,
        "required_source_groups": run["news_policy"]["required_source_groups"],
        "sources": [{"source_id": source, "urls": urls}], "max_urls_per_source": len(urls),
        **{key: params[key] for key in ("timeout_seconds", "budget_seconds", "max_pages_per_source")}}}


def _seal_followup(store, artifacts, value):
    fields(value, {"schema_id", "task_id", "workflow_id", "run_id", "retrieval_operation_id", "retrieval_result_ref",
        "window_start", "source_id", "lineage", "request", "prior_cost_operation_ids", "prior_costs", "prior_class_costs",
        "resource_limits", "class_limits"}, label="source followup preflight declaration")
    require(store.operation(value["workflow_id"]+":collect") is None,
            "Completed source aggregation cannot append a new followup task")
    require(value["schema_id"] == FOLLOWUP_TASK_SCHEMA and value["task_id"] == value["request"]["request_id"]+":task",
            "Source followup preregistration identity differs from its exact request")
    request = {"schema_id": FOLLOWUP_TASK_SCHEMA, "task": value}
    identity = value["task_id"]
    saved = store.begin(identity, request)
    require(saved["request"] == request, "Source followup changed its preregistered task")
    if saved["status"] == "completed":
        reference = saved["result"]["task_ref"]
        require(artifacts.read_json(reference) == {**value, "planned_at": saved["created_at"]},
                "Source followup seal changed")
        return reference
    task = {**value, "planned_at": saved["created_at"]}
    reference = artifacts.put_json(task)
    with store.lease(identity):
        store.complete(identity, {"task_ref": reference}, writes=[
            {"kind": "news-derived-task", "key": identity,
             "value": {"workflow_id": value["workflow_id"], "task_ref": reference}, "immutable": True}])
    return reference


def _validate_task(reference, original, run, registry, params, prior_children, prior_ids, store, artifacts, gkg,
                   *, window_start, graph_memo=None, cost_memo=None):
    task = artifacts.read_json(reference)
    fields(task, {"schema_id", "task_id", "workflow_id", "run_id", "retrieval_operation_id", "retrieval_result_ref",
        "window_start", "source_id", "lineage", "request", "prior_cost_operation_ids", "prior_costs", "prior_class_costs",
        "resource_limits", "class_limits", "planned_at"}, label="preregistered source followup")
    declaration = store.operation(task["task_id"])
    body = {key: value for key, value in task.items() if key != "planned_at"}
    require(declaration is not None and declaration["status"] == "completed"
            and declaration["request"] == {"schema_id": FOLLOWUP_TASK_SCHEMA, "task": body}
            and declaration["result"] == {"task_ref": reference}
            and declaration["created_at"] == task["planned_at"]
            and store.get("news-derived-task", task["task_id"]) == {"workflow_id": task["workflow_id"], "task_ref": reference},
            "Source followup lacks its original preregistration journal")
    require(task["schema_id"] == FOLLOWUP_TASK_SCHEMA and task["workflow_id"] == original["workflow_id"]
            and task["run_id"] == run["run_id"] and task["retrieval_operation_id"] == original["retrieval_id"]
            and task["window_start"] == window_start
            and artifacts.read_json(task["retrieval_result_ref"]) == store.operation(original["retrieval_id"])["result"],
            "Source followup replaced its original retrieval or run")
    require(original["resource_plan"].get("budget_scope") == FOLLOWUP_BUDGET_SCOPE,
            "Original retrieval resource scope does not authorize source-derived followups")
    capacity, class_limits = _limits(original, params)
    prior_costs, prior_classes = _costs(prior_ids, store, artifacts, gkg, memo=cost_memo)
    require(task["prior_cost_operation_ids"] == prior_ids and task["prior_costs"] == prior_costs
            and task["prior_class_costs"] == prior_classes and task["resource_limits"] == capacity
            and task["class_limits"] == class_limits, "Source followup reset or altered its cumulative original budget")
    edges, captures, listings, _ = _link_domain(prior_children, original, run, registry, artifacts, store, memo=graph_memo)
    source = task["source_id"]
    require(source in run["news_policy"]["sources"] and task["lineage"]
            and all(edge in edges.get((source, edge["url"]), []) and edge["source_id"] == source for edge in task["lineage"]),
            "Source followup URL is not a registered link in its actual parent original")
    urls = []
    new_listings = set()
    for edge in task["lineage"]:
        require((source, edge["url"]) not in captures, "Source followup repeated an already attempted URL")
        parent = next(row for child, manifest in prior_children if child["public_operation_id"] == edge["parent_capture"]["public_operation_id"]
            for row in manifest["retrievals"] if row["retrieval_id"] == edge["parent_capture"]["retrieval_id"])
        parent_operation = store.operation(edge["parent_capture"]["public_operation_id"])
        require(instant(parent["retrieved_at"]) <= instant(task["planned_at"])
                and instant(parent_operation["updated_at"]) <= instant(task["planned_at"]),
                "Source followup was planned before its original parent was accepted")
        if edge["kind"] == "pdf":
            urls.append(edge["parent_capture"]["final_url"])
        urls.append(edge["url"])
        if edge["kind"] == "listing":
            new_listings.add(edge["url"])
    require(len(urls) == len(set(urls)) and 0 < len(urls) <= params["max_urls_per_source"],
            "Source followup changed or duplicated its atomic URL batch")
    require(not new_listings or len(listings.get(source, set()) | new_listings) <= params["max_pages_per_source"],
            "Source followup reset its cumulative publisher listing page allowance")
    require(task["request"] == _followup_request(task["request"]["request_id"], run, task["window_start"], source, urls, params),
            "Source followup request is not its exact source-bound atomic task")
    worst = {"requests": len(urls)*MAX_PRODUCER_ATTEMPTS, "bytes": len(urls)*2*1024*1024*MAX_PRODUCER_ATTEMPTS,
             "elapsed_seconds": params["budget_seconds"]*MAX_PRODUCER_ATTEMPTS}
    kind = "GKG" if source in gkg else "official"
    require(prior_classes[kind]+worst["requests"] <= class_limits[kind]
            and all(prior_costs[key]+amount <= capacity[key] for key, amount in worst.items()),
            "Source followup was not authorized by its remaining cumulative original budget")
    child = store.operation(task["request"]["request_id"])
    if child is not None:
        require(child["request"] == task["request"] and instant(declaration["updated_at"]) <= instant(child["created_at"]),
                "Source followup was sealed after capture began or changed its exact public request")
    return task


def validate_followup_scope(payload, run, registry, store, artifacts):
    """Independently authenticate the full derived task graph and complement."""
    reference = payload.get("derived_source_tasks_ref")
    if reference is None:
        return {}
    from news_scope import accepted_retrieval
    index = artifacts.read_json(reference)
    fields(index, {"schema_id", "workflow_id", "run_id", "retrieval_operation_id", "retrieval_result_ref", "parameters",
        "window_start", "initial_children", "tasks", "pending", "spent", "class_spent", "scope"}, label="source followup inventory")
    original = accepted_retrieval(payload["retrieval_operation_id"], run, registry, store, artifacts)
    require(index["schema_id"] == FOLLOWUP_SCHEMA and index["workflow_id"] == original["workflow_id"]
            and index["run_id"] == run["run_id"] and index["retrieval_operation_id"] == original["retrieval_id"]
            and index["window_start"] == payload["window_start"]
            and artifacts.read_json(index["retrieval_result_ref"]) == store.operation(original["retrieval_id"])["result"]
            and index["scope"] == "actual_source_link_graph_with_complete_observed_pending_complement",
            "Derived source inventory changed its original retrieval or window")
    params = _parameters(index["parameters"])
    gkg = set(artifacts.read_json(original["GKG_source_ids_ref"]))
    expected_initial = []
    children, prior_ids = [], []
    for number, selected in enumerate(_selected_tasks(original, params, gkg), 1):
        identity = original["workflow_id"]+":news-shard:"+str(number).zfill(6)
        operation = store.operation(identity)
        if operation is None:
            continue
        require(operation["status"] == "completed", "Source inventory omitted an unfinished original selected operation")
        supplied = next((row for row in index["initial_children"] if row["public_operation_id"] == identity), None)
        require(supplied is not None, "Source inventory omitted an original selected child")
        row, manifest = _child_reference(store, artifacts, identity, result_ref=supplied["result_ref"])
        require(operation["request"] == _followup_request(identity, run, payload["window_start"], selected["source_id"], selected["urls"], params),
                "Source inventory altered an original selected request")
        expected_initial.append(row)
        children.append((row, manifest)); prior_ids.append(identity)
    require(index["initial_children"] == expected_initial, "Source inventory omitted or replaced an original selected child")
    declarations = {value["task_ref"]["sha256"] for _, value in store.scan("news-derived-task") if value["workflow_id"] == index["workflow_id"]}
    require(type(index["tasks"]) is list and len({row["task_ref"]["sha256"] for row in index["tasks"]}) == len(index["tasks"]),
            "Source followup tasks are duplicated or invalid")
    require(declarations == {row["task_ref"]["sha256"] for row in index["tasks"]}, "Source inventory omitted a preregistered derived task")
    allowed, graph_memo, cost_memo = {}, {}, {}
    for number, row in enumerate(index["tasks"], 1):
        fields(row, {"task_ref", "child"}, label="source followup task receipt")
        task = _validate_task(row["task_ref"], original, run, registry, params, children, prior_ids, store, artifacts, gkg,
            window_start=payload["window_start"], graph_memo=graph_memo, cost_memo=cost_memo)
        identity = index["workflow_id"]+":news-followup:"+str(number).zfill(6)
        require(task["task_id"] == identity+":task" and task["request"]["request_id"] == identity,
                "Source followup task order or exact identity changed")
        if row["child"] is None:
            require(store.operation(identity) is None, "Source inventory hid an attempted derived operation")
            continue
        manifest = _reference_child(row["child"], store, artifacts, run, payload["window_start"])
        require(row["child"]["public_operation_id"] == identity, "Derived receipt belongs to another preregistered operation")
        allowed[manifest["collection_id"]] = {task["source_id"]: set(task["request"]["payload"]["sources"][0]["urls"])}
        children.append((row["child"], manifest)); prior_ids.append(identity)
    pending = _link_domain(children, original, run, registry, artifacts, store, memo=graph_memo)[3]
    spent, class_spent = _costs(prior_ids, store, artifacts, gkg, memo=cost_memo)
    capacity, class_limits = _limits(original, params)
    require(index["pending"] == pending and index["spent"] == spent and index["class_spent"] == class_spent,
            "Source inventory changed or omitted its actual pending complement or original attempt costs")
    require(all(spent[key] <= amount for key, amount in capacity.items())
            and all(class_spent[key] <= amount for key, amount in class_limits.items()),
            "Source followups exceeded their cumulative original budget")
    require(set(payload["collection_ids"]) == {manifest["collection_id"] for _, manifest in children},
            "Source aggregate omitted or duplicated an original or derived child")
    return allowed


def _followup_inventory(journal, original, run, registry, params, store, artifacts, gkg):
    """Only this scheduler plans and executes the source-derived queue."""
    import pipeline
    from contracts import utc_now
    initial, children, prior_ids, tasks = [], [], [], []
    for batch in journal["batches"]:
        if not batch.get("completed"):
            continue
        row, manifest = _child_reference(store, artifacts, batch["request"]["request_id"])
        if batch.get("role") == "source_followup":
            tasks.append({"task_ref": batch["task_ref"], "child": row})
        else:
            initial.append(row)
        children.append((row, manifest)); prior_ids.append(row["public_operation_id"])
    original_operation = store.operation(original["retrieval_id"])
    retrieval_ref = artifacts.put_json(original_operation["result"])
    capacity, class_limits = _limits(original, params)
    sources = sorted(run["news_policy"]["sources"])
    next_source = ((sources.index(artifacts.read_json(tasks[-1]["task_ref"])["source_id"])+1) % len(sources)) if tasks else 0
    graph_memo, cost_memo = {}, {}
    while True:
        edges, captures, listings, pending = _link_domain(children, original, run, registry, artifacts, store, memo=graph_memo)
        spent, classes = _costs(prior_ids, store, artifacts, gkg, memo=cost_memo)
        resumed = next((batch for batch in journal["batches"]
            if batch.get("role") == "source_followup" and not batch.get("completed")), None)
        if resumed is not None:
            task = _validate_task(resumed["task_ref"], original, run, registry, params,
                children, prior_ids, store, artifacts, gkg, window_start=journal["contract"]["window_start"],
                graph_memo=graph_memo, cost_memo=cost_memo)
            request, task_ref = task["request"], resumed["task_ref"]
            source = task["source_id"]
            batch = resumed
        else:
            if original["resource_plan"].get("budget_scope") != FOLLOWUP_BUDGET_SCOPE:
                journal["followup_reason"] = "followup_not_authorized_by_original_resource_scope"
                break
            chosen = None
            for offset in range(len(sources)):
                source_index = (next_source+offset) % len(sources)
                source = sources[source_index]
                available = [(key, values) for key, values in edges.items() if key[0] == source and key not in captures]
                available.sort(key=lambda pair: (min({"article": 0, "pdf": 1, "listing": 2}[edge["kind"]] for edge in pair[1]), pair[0][1]))
                lineage, urls, new_listings = [], [], set()
                for key, values in available:
                    edge = sorted(values, key=lambda value: (value["parent_capture"]["public_operation_id"], value["parent_capture"]["retrieval_id"]))[0]
                    added = ([edge["parent_capture"]["final_url"]] if edge["kind"] == "pdf" else [])+[edge["url"]]
                    if any(url in urls for url in added) or len(urls)+len(added) > params["max_urls_per_source"]:
                        continue
                    if edge["kind"] == "listing" and len(listings.get(source, set()) | new_listings | {edge["url"]}) > params["max_pages_per_source"]:
                        continue
                    worst = {"requests": (len(urls)+len(added))*MAX_PRODUCER_ATTEMPTS,
                        "bytes": (len(urls)+len(added))*2*1024*1024*MAX_PRODUCER_ATTEMPTS,
                        "elapsed_seconds": params["budget_seconds"]*MAX_PRODUCER_ATTEMPTS}
                    kind = "GKG" if source in gkg else "official"
                    if classes[kind]+worst["requests"] > class_limits[kind] or any(spent[name]+amount > capacity[name] for name, amount in worst.items()):
                        continue
                    lineage.append(edge); urls.extend(added)
                    if edge["kind"] == "listing":
                        new_listings.add(edge["url"])
                    # A PDF has its fresh parent as an atomic dependency;
                    # other descendants wait for the actual new capture.
                    if edge["kind"] == "pdf":
                        break
                if lineage:
                    chosen = source, lineage, urls
                    next_source = (source_index+1) % len(sources)
                    break
            if chosen is None:
                journal["followup_reason"] = ("actual_source_graph_attempted_remaining_links_or_failures_explicit"
                    if pending else "actual_source_graph_attempted_global_window_exhaustiveness_unknown")
                break
            source, lineage, urls = chosen
            number = len(tasks)+1
            identity = original["workflow_id"]+":news-followup:"+str(number).zfill(6)
            request = _followup_request(identity, run, journal["contract"]["window_start"], source, urls, params)
            # All parent derivations, URL identities and budget reservations
            # above are checked before creating this immutable task identity.
            planned_clock = instant(utc_now())
            require(all(instant(store.operation(edge["parent_capture"]["public_operation_id"])["updated_at"]) <= planned_clock
                for edge in lineage), "Source followup parent acceptance is in the future")
            value = {"schema_id": FOLLOWUP_TASK_SCHEMA, "task_id": identity+":task", "workflow_id": original["workflow_id"],
                "run_id": run["run_id"], "retrieval_operation_id": original["retrieval_id"], "retrieval_result_ref": retrieval_ref,
                "window_start": journal["contract"]["window_start"], "source_id": source, "lineage": lineage, "request": request,
                "prior_cost_operation_ids": list(prior_ids), "prior_costs": spent, "prior_class_costs": classes,
                "resource_limits": capacity, "class_limits": class_limits}
            task_ref = _seal_followup(store, artifacts, value)
            _validate_task(task_ref, original, run, registry, params, children, prior_ids, store, artifacts, gkg,
                window_start=journal["contract"]["window_start"], graph_memo=graph_memo, cost_memo=cost_memo)
            batch = {"role": "source_followup", "source_id": source, "request": request, "task_ref": task_ref}
            journal["batches"].append(batch)
            write_artifact_json(store.base/"workflow-news"/(fingerprint({"workflow_id": original["workflow_id"]})+".json"), journal)
        pipeline.run(request, store.root, store.plan)
        row, manifest = _child_reference(store, artifacts, request["request_id"])
        batch.update(completed=True, collection_id=manifest["collection_id"])
        tasks.append({"task_ref": task_ref, "child": row})
        children.append((row, manifest)); prior_ids.append(row["public_operation_id"])
        spent, classes = _costs(prior_ids, store, artifacts, gkg, memo=cost_memo)
        require(all(spent[name] <= amount for name, amount in capacity.items())
                and all(classes[name] <= amount for name, amount in class_limits.items()),
                "Source-derived attempts exceeded their cumulative original budget")
        journal["consumed"] = spent
        journal["class_source_requests_consumed"] = classes
        write_artifact_json(store.base/"workflow-news"/(fingerprint({"workflow_id": original["workflow_id"]})+".json"), journal)
    spent, classes = _costs(prior_ids, store, artifacts, gkg, memo=cost_memo)
    pending = _link_domain(children, original, run, registry, artifacts, store, memo=graph_memo)[3]
    value = {"schema_id": FOLLOWUP_SCHEMA, "workflow_id": original["workflow_id"], "run_id": run["run_id"],
        "retrieval_operation_id": original["retrieval_id"], "retrieval_result_ref": retrieval_ref, "parameters": params,
        "window_start": journal["contract"]["window_start"], "initial_children": initial, "tasks": tasks,
        "pending": pending, "spent": spent, "class_spent": classes,
        "scope": "actual_source_link_graph_with_complete_observed_pending_complement"}
    return artifacts.put_json(value), spent, classes


def _collect_selected(root, plan, run, workflow_id, window_start, params, retrieval, store, artifacts, registry):
    """Capture the accepted finite transport tasks, preserving its full domain."""
    import pipeline
    from news_scope import accepted_retrieval
    identity = retrieval["retrieval_id"]
    original = accepted_retrieval(identity, run, registry, store, artifacts)
    require({key: value for key, value in retrieval.items() if key != "automatic_validation"} == original,
            "News workflow changed the accepted public retrieval selection")
    gkg = set(artifacts.read_json(original["GKG_source_ids_ref"]))
    require(gkg <= set(run["news_policy"]["sources"]), "Retrieval GKG protocols differ from the frozen source domain")
    # Preserve every original selected transport task. Followups receive
    # separate preregistered identities and do not rewrite this selection.
    tasks = _selected_tasks(original, params, gkg)
    capacity, class_limits = _limits(original, params)
    contract = {"workflow_id": workflow_id, "run_hash": fingerprint(run), "window_start": window_start,
                "parameters": params, "retrieval_operation_id": identity, "retrieval_hash": fingerprint(original)}
    path = store.base/"workflow-news"/(fingerprint({"workflow_id": workflow_id})+".json")
    with _lock(path.with_suffix(".lock")):
        journal = read_artifact_json(path) if path.exists() else None
        if journal:
            require(journal["contract"] == contract, "News workflow changed its frozen retrieval or resource budget")
        else:
            journal = {"contract": contract, "workflow_id": workflow_id, "sources": sorted(run["news_policy"]["sources"]),
                "batches": [], "resource_plan": {"limits": capacity, "class_source_request_limits": class_limits,
                    "basis": "accepted_retrieval_actual_source_attempts_including_failures_and_original_retry_ceiling", "world_article_total": None},
                "consumed": {}, "status": "collecting"}
            write_artifact_json(path, journal)
        if journal["status"] != "collecting":
            operation = store.operation(workflow_id+":collect")
            require(operation and operation["status"] == "completed", "Final selected news aggregate is not committed")
            _accepted(store, artifacts, workflow_id+":collect")
            return _receipt(journal, operation["result"])
        consumed = {"requests": 0, "bytes": 0, "elapsed_seconds": 0.0, "producer_attempts": 0}
        class_used = {"GKG": 0, "official": 0}
        costs = {}
        for batch in journal["batches"]:
            cost = _cost(store, artifacts, batch["request"])
            costs[batch["request"]["request_id"]] = cost
            for key, amount in cost.items():
                consumed[key] += amount
            class_used["GKG" if batch["source_id"] in gkg else "official"] += cost["requests"]
            if batch.get("completed"):
                _accepted(store, artifacts, batch["request"]["request_id"])
        journal["consumed"] = consumed
        for number, task in enumerate(tasks, 1):
            request_id = workflow_id+":news-shard:"+str(number).zfill(6)
            batch = next((row for row in journal["batches"] if row["request"]["request_id"] == request_id), None)
            if batch and batch.get("completed"):
                continue
            # Explicit finite seeds need no larger initial fetch allowance.
            # Any discovered link left outside this shard remains in its
            # immutable child manifest; no PDF or follow-up fact is inferred.
            url_budget = min(params["max_urls_per_source"], len(task["urls"]))
            request = {"schema_version": 4, "request_id": request_id, "operation": "news_collect", "payload": {
                "run_id": run["run_id"], "cutoff_at": run["publish_cutoff"], "window_start": window_start,
                "required_source_groups": run["news_policy"]["required_source_groups"], "sources": [task],
                "max_urls_per_source": url_budget, **{key: params[key] for key in ("timeout_seconds", "budget_seconds", "max_pages_per_source")}}}
            require(batch is None or batch["request"] == request, "Resumed news task changed its original explicit URLs")
            prior = costs.get(request_id, {"requests": 0, "bytes": 0, "elapsed_seconds": 0., "producer_attempts": 0})
            remaining = max(0, MAX_PRODUCER_ATTEMPTS-prior["producer_attempts"])
            worst = {"requests": url_budget*remaining, "bytes": url_budget*2*1024*1024*remaining,
                     "elapsed_seconds": params["budget_seconds"]*remaining}
            kind = "GKG" if task["source_id"] in gkg else "official"
            if (class_used[kind]+worst["requests"] > class_limits[kind]
                    or any(consumed[key]+amount > capacity[key] for key, amount in worst.items())):
                journal["reason"] = "selected_retrieval_actual_attempt_budget_reserved_with_original_scope_pending"
                continue
            if batch is None:
                batch = {"source_id": task["source_id"], "request": request}
                journal["batches"].append(batch)
            write_artifact_json(path, journal)
            pipeline.run(request, root, plan)
            value = _accepted(store, artifacts, request_id)
            cost = _cost(store, artifacts, request)
            for key, amount in cost.items():
                consumed[key] += amount-prior[key]
            class_used[kind] += cost["requests"]-prior["requests"]
            require(class_used[kind] <= class_limits[kind]
                    and all(consumed[key] <= capacity[key] for key in capacity),
                    "Original selected news attempts exceeded their frozen cumulative budget")
            batch.update(completed=True, collection_id=value["collection_id"])
            write_artifact_json(path, journal)
        derived_ref, consumed, class_used = _followup_inventory(journal, original, run, registry, params, store, artifacts, gkg)
        journal["consumed"] = consumed
        completed = [batch for batch in journal["batches"] if batch.get("completed")]
        unexamined = sorted(set(journal["sources"])-{batch["source_id"] for batch in completed})
        request = {"schema_version": 4, "request_id": workflow_id+":collect", "operation": "news_collect", "payload": {
            "run_id": run["run_id"], "cutoff_at": run["publish_cutoff"], "window_start": window_start,
            "required_source_groups": run["news_policy"]["required_source_groups"],
            "collection_ids": [batch["collection_id"] for batch in completed], "unexamined_source_ids": unexamined,
            "retrieval_operation_id": identity, "derived_source_tasks_ref": derived_ref}}
        result = pipeline.run(request, root, plan)
        _accepted(store, artifacts, request["request_id"])
        journal.update(status="selected_retrieval_completed_original_scope_pending", class_source_requests_consumed=class_used,
            reason=journal.get("reason") or "original_unselected_domain_and_economic_query_tasks_remain_explicit")
        write_artifact_json(path, journal)
        return _receipt(journal, result)


def collect(root, plan, run, workflow_id, window_start, *, parameters=None, retrieval=None):
    import news
    import pipeline
    store = Store(root, plan); artifacts = Artifacts(store.base)
    current, registry = news.run_policy(run["run_id"], store, artifacts)
    require(current == run, "News workflow cannot replace its frozen analysis run")
    params = _parameters(parameters)
    if retrieval is not None:
        return _collect_selected(root, plan, run, workflow_id, window_start, params, retrieval, store, artifacts, registry)
    sources = sorted(run["news_policy"]["sources"])
    contract = {"workflow_id": workflow_id, "run_hash": fingerprint(run), "window_start": window_start, "parameters": params}
    path = store.base/"workflow-news"/(fingerprint({"workflow_id": workflow_id})+".json")
    with _lock(path.with_suffix(".lock")):
        journal = read_artifact_json(path) if path.exists() else None
        if journal:
            require(journal["contract"] == contract, "News workflow changed its frozen source budget or window")
        else:
            # Reserve one complete initial pass, then the source-derived link
            # inventories of every publisher. This budget is engineering
            # capacity, never evidence that those inventories are exhaustive.
            seeds = sum(len(registry["sources"][source]["default_urls"]) for source in sources)
            request_units = seeds+len(sources)*params["max_pages_per_source"]*5000
            capacity = {"requests": request_units*MAX_PRODUCER_ATTEMPTS, "bytes": request_units*2*1024*1024*MAX_PRODUCER_ATTEMPTS,
                "elapsed_seconds": math.ceil(request_units/params["max_urls_per_source"])*params["budget_seconds"]*MAX_PRODUCER_ATTEMPTS}
            for parameter, key in (("max_total_source_requests", "requests"), ("max_total_bytes", "bytes"), ("max_total_elapsed_seconds", "elapsed_seconds")):
                if parameter in params:
                    capacity[key] = min(capacity[key], params[parameter])
            journal = {"contract": contract, "workflow_id": workflow_id, "sources": sources, "batches": [],
                "resource_plan": {"limits": capacity, "basis": "all_frozen_publisher_channels_times_per_original_page_parser_budget_and_retry_ceiling", "world_article_total": None},
                "consumed": {"requests": 0, "bytes": 0, "elapsed_seconds": 0.0, "producer_attempts": 0}, "status": "collecting"}
            write_artifact_json(path, journal)
        if journal["status"] != "collecting":
            operation = store.operation(workflow_id+":collect")
            require(operation and operation["status"] == "completed", "Final news aggregate is not committed")
            _accepted(store, artifacts, workflow_id+":collect")
            return _receipt(journal, operation["result"])
        pending = list(sources)
        seen = {source: set() for source in sources}
        failed = set()
        for batch in journal["batches"]:
            if batch.get("completed"):
                value = _accepted(store, artifacts, batch["request"]["request_id"])
                source = batch["source_id"]
                seen[source].update(row.get("version_id") or row["requested_url"] for row in value["retrievals"] if row["state"] in {"body", "lead"})
                if source in pending:
                    pending.remove(source)
                if batch.get("progress") and value["source_results"][0]["discovery_pending_urls"]:
                    pending.append(source)
                elif value["source_results"][0]["discovery_pending_urls"] or not value["source_results"][0]["advance_cursor"]:
                    failed.add(source)
        # Rebuild charges from original attempt journals after interruption;
        # never trust the mutable engineering progress file as a budget seal.
        journal["consumed"] = {"requests": 0, "bytes": 0, "elapsed_seconds": 0.0, "producer_attempts": 0}
        for original_batch in journal["batches"]:
            for key, amount in _cost(store, artifacts, original_batch["request"]).items():
                journal["consumed"][key] += amount
        while pending:
            source = pending.pop(0)
            resumed = next((batch for batch in journal["batches"] if batch["source_id"] == source and not batch.get("completed")), None)
            request_id = resumed["request"]["request_id"] if resumed else workflow_id+":news-shard:"+str(len(journal["batches"])+1).zfill(6)
            batch = resumed or {"source_id": source, "request": {"schema_version": 4, "request_id": request_id, "operation": "news_collect", "payload": {
                "run_id": run["run_id"], "cutoff_at": run["publish_cutoff"], "window_start": window_start,
                "required_source_groups": run["news_policy"]["required_source_groups"], "source_ids": [source],
                **{key: params[key] for key in ("timeout_seconds", "budget_seconds", "max_urls_per_source", "max_pages_per_source")}}}}
            prior_cost = _cost(store, artifacts, batch["request"]) if resumed else {"producer_attempts": 0}
            remaining_attempts = max(0, MAX_PRODUCER_ATTEMPTS-prior_cost["producer_attempts"])
            worst = {"requests": params["max_urls_per_source"]*remaining_attempts,
                "bytes": params["max_urls_per_source"]*2*1024*1024*remaining_attempts,
                "elapsed_seconds": params["budget_seconds"]*remaining_attempts}
            if any(journal["consumed"][key]+value > journal["resource_plan"]["limits"][key] for key, value in worst.items()):
                journal["reason"] = "global_news_budget_exhausted_with_original_scope_pending"; break
            if not resumed:
                journal["batches"].append(batch)
            write_artifact_json(path, journal)
            result = pipeline.run(batch["request"], root, plan)
            value = _accepted(store, artifacts, request_id)
            cost = _cost(store, artifacts, batch["request"])
            for key, amount in cost.items():
                journal["consumed"][key] += amount-prior_cost.get(key, 0)
            observed = {row.get("version_id") or row["requested_url"] for row in value["retrievals"] if row["state"] in {"body", "lead"}}
            original_pending = value["source_results"][0]["discovery_pending_urls"]
            progress = bool(observed-seen[source]); seen[source].update(observed)
            batch.update(completed=True, progress=progress, collection_id=value["collection_id"])
            if progress and original_pending:
                pending.append(source)
            elif original_pending:
                failed.add(source)
            elif not value["source_results"][0]["advance_cursor"]:
                failed.add(source)
            write_artifact_json(path, journal)
        # A full-policy aggregate is mandatory. A parent budget too small even
        # for the initial domain remains an explicit failure, not a subset win.
        completed = [batch for batch in journal["batches"] if batch.get("completed")]
        unexamined = sorted(set(sources)-{batch["source_id"] for batch in completed})
        if unexamined:
            journal.update(status="publisher_domain_incomplete", reason=journal.get("reason") or "original_publisher_source_pass_incomplete")
        else:
            journal["status"] = "source_inventory_traversed_window_exhaustiveness_unknown" if not pending and not failed else "news_coverage_incomplete"
        journal["reason"] = journal.get("reason") or ("publisher_protocol_or_original_pagination_pending" if failed else "global_news_and_publisher_window_denominators_unknown")
        request = {"schema_version": 4, "request_id": workflow_id+":collect", "operation": "news_collect", "payload": {
            "run_id": run["run_id"], "cutoff_at": run["publish_cutoff"], "window_start": window_start,
            "required_source_groups": run["news_policy"]["required_source_groups"], "collection_ids": [batch["collection_id"] for batch in completed],
            "unexamined_source_ids": unexamined}}
        result = pipeline.run(request, root, plan)
        _accepted(store, artifacts, request["request_id"])
        write_artifact_json(path, journal)
        return _receipt(journal, result)
