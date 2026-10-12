"""Source-bound, finite retrieval planning; metadata never supplies news facts.

Every original source and parsed row remains in its immutable opportunity
domain. This module schedules exact probe URLs for subsequent public capture.
It performs no network requests, source qualification, sector assignment or
financial prediction. The budget and ordering are engineering heuristics.
"""
import bisect
import copy
import hashlib
import heapq
import re
from collections import Counter
from itertools import zip_longest

from contracts import fields, fingerprint, require

POLICY_ID = "broad_economic_opportunity_retrieval_v1"
GKG_SOURCE_REQUEST_BUDGET = 192
URLS_PER_BATCH = 8
MAX_PRODUCER_ATTEMPTS = 9
# Transmission mechanisms, not an industry, publisher or fund whitelist.
# A match only prioritizes a question. Non-matches remain query tasks too.
MECHANISM_WORDS = frozenset("ECON ECONOMY ECONOMIC FINANCE FINANCIAL INFLATION PRICE PRICES "
    "RATE RATES INTEREST CREDIT DEBT DEFAULT BANK BANKING MONETARY FISCAL TAX TAXES "
    "TRADE TARIFF TARIFFS EXPORT IMPORT CURRENCY EXCHANGE SUPPLY DEMAND PRODUCTION "
    "CONSUMPTION EMPLOYMENT UNEMPLOYMENT WAGE WAGES INVESTMENT POLICY REGULATION "
    "SANCTION SANCTIONS ENERGY COMMODITY COMMODITIES TRANSPORT LOGISTICS".split())
QUESTION = ("Read original reporting to determine whether this topic has a supported "
    "mechanism through supply, demand, income, prices, policy, interest rates, credit, "
    "trade or exchange rates; seek contrary evidence and unknown effects.")


def _rank(*parts):
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _mechanism(theme):
    return bool(set(re.findall(r"[A-Z]+", (theme or "").upper())) & MECHANISM_WORDS)


def _propose(task, seed, source, url):
    """Keep the same bounded proposals without repeatedly scanning 192 ranks.

    Negative hash rank makes the heap root the old maximum. Insertion order
    breaks exact ties as Python's original max(dict, key=rank) did. Only the
    retained source proposals carry cached URL ranks; no domain is filtered.
    """
    proposals, ranks = task["proposals"], task["source_ranks"]
    task_key = task["query_id"]
    heap, url_ranks = task["source_heap"], task["proposal_url_ranks"]
    if source not in proposals:
        rank = _rank(seed, task_key, source)
        if len(proposals) >= GKG_SOURCE_REQUEST_BUDGET:
            worst = heap[0][2]
            if rank >= ranks[worst]:
                return
            heapq.heappop(heap)
            del proposals[worst]; del ranks[worst]; del url_ranks[worst]
        ranks[source] = rank
        proposals[source] = {"source_id": source, "url": url}
        url_ranks[source] = _rank(seed, task_key, url)
        heapq.heappush(heap, (-int(rank, 16), task["proposal_sequence"], source))
        task["proposal_sequence"] += 1
    elif url != proposals[source]["url"]:
        url_rank = _rank(seed, task_key, url)
        if url_rank < url_ranks[source]:
            proposals[source] = {"source_id": source, "url": url}
            url_ranks[source] = url_rank


def _request(payload):
    fields(payload, {"workflow_id", "publisher_discovery_id"}, label="news retrieval request")
    require(all(type(payload[key]) is str and payload[key] for key in payload),
            "Retrieval workflow and publisher identities must be nonempty text")
    require(payload["publisher_discovery_id"] == payload["workflow_id"]+":publishers",
            "Retrieval must use this workflow's original public publisher discovery")
    return copy.deepcopy(payload)


def _inputs(payload, store, artifacts, replay_originals):
    discovery_id = payload["publisher_discovery_id"]
    operation = store.operation(discovery_id)
    require(operation is not None and operation["status"] == "completed"
            and operation["request"]["operation"] == "publisher_discover",
            "Retrieval needs a completed public publisher discovery")
    publisher = store.get("publisher-discovery", discovery_id)
    require(publisher is not None and publisher["discovery_id"] == discovery_id
            and publisher["request"] == operation["request"]["payload"]
            and publisher["request_hash"] == fingerprint(publisher["request"]),
            "Retrieval publisher differs from its canonical public request")
    require(all(operation["result"].get(key) == value for key, value in publisher.items()),
            "Retrieval publisher differs from the completed public accepted result")
    # Every source consumption authenticates the completed public boundary and
    # rereads all current CAS bytes. Its already accepted publisher semantics
    # need not be replayed; our own retrieval is independently rebuilt below.
    import stage_validation
    registry = stage_validation.validated_publisher_registry(discovery_id, store, artifacts)
    gdelt = None
    if publisher.get("gdelt_discovery_id"):
        gdelt = store.get("gdelt-opportunity-discovery", publisher["gdelt_discovery_id"])
        require(gdelt is not None and artifacts.read_json(publisher["gdelt_opportunity_ref"]) == gdelt
                and gdelt["coverage"] == publisher["gdelt_coverage"]
                and gdelt["request"]["window_start"] == publisher["gdelt_window"]["window_start"]
                and gdelt["request"]["window_end"] == publisher["gdelt_window"]["window_end"],
                "Retrieval GKG domain differs from the original public publisher window")
    return publisher, registry, gdelt


def _build(payload, publisher, registry, gdelt, artifacts, operation_id, emit):
    sources = {name: rule for name, rule in registry["sources"].items()
               if rule["purpose"] == "news" and rule.get("default_urls")}
    gkg_sources = (artifacts.read_json(gdelt["source_registry_ref"])["sources"] if gdelt else {})
    require(set(gkg_sources) <= set(sources), "GKG opportunity source missing from original publisher registry")
    require(all(sources[name] == rule for name, rule in gkg_sources.items()),
            "GKG opportunity registry differs from its original source partition")
    official = {name: rule for name, rule in sources.items() if name not in gkg_sources}
    # Source lists, rather than another million-key dictionary, define exact
    # membership. One byte per probe URL records metadata routing status.
    flags, host_sources = {}, {}
    for source, rule in gkg_sources.items():
        urls = rule["default_urls"]
        require(urls == sorted(set(urls)), "Original GKG probe URL domain is not exact and ordered")
        flags[source] = bytearray(len(urls))
        require(len(rule["allowed_hosts"]) == 1, "Original GKG publisher host is ambiguous")
        host_sources[rule["allowed_hosts"][0]] = source
    seed = fingerprint({"policy": POLICY_ID, "workflow_id": payload["workflow_id"],
                        "publisher_request_hash": publisher["request_hash"]})
    tasks, theme_tasks = {}, {}
    row_counts = Counter({name: 0 for name in ("original_metadata_rows", "unsupported_metadata_rows",
        "candidate_metadata_rows", "ambiguous_metadata_rows", "valid_web_opportunity_rows")})
    parsed_parts = []
    file_states = []

    def query_task(theme):
        if theme in theme_tasks:
            return theme_tasks[theme]
        task_key = fingerprint({"machine_theme": theme})
        task = {"query_id": task_key, "machine_theme": theme,
            "question": QUESTION, "metadata_priority_hint": "mechanism_word_candidate" if _mechanism(theme) else "ambiguous_topic",
            "matched_original_rows": 0, "matched_valid_web_rows": 0,
            "proposals": {}, "source_ranks": {}, "source_heap": [],
            "proposal_url_ranks": {}, "proposal_sequence": 0}
        tasks[task_key] = task
        theme_tasks[theme] = task
        return task

    for part in (gdelt["parsed_files"] if gdelt else []):
        file_states.append({key: part[key] for key in ("file_id", "status")})
        if part["status"] != "captured":
            continue
        parsed_parts.append({"file_id": part["file_id"], "parsed_ref": part["parsed_ref"]})
        parsed = artifacts.read_json(part["parsed_ref"])
        require(parsed["metadata_rows"] == len(parsed["records"])+len(parsed["excluded_records"]),
                "Original GKG row denominator differs from retained supported and unsupported rows")
        row_counts["original_metadata_rows"] += parsed["metadata_rows"]
        row_counts["unsupported_metadata_rows"] += len(parsed["excluded_records"])
        # The original parser's inventory includes themes on excluded/non-web
        # rows too. Keep their questions pending without inventing article URLs.
        for theme, count in sorted(parsed["all_observed_theme_counts"].items()):
            require(type(theme) is str and theme and type(count) is int and count > 0,
                    "Original GKG theme inventory is unsupported")
            query_task(theme)["matched_original_rows"] += count
        for row in parsed["records"]:
            source = host_sources.get(row["publisher_host"])
            require(source is not None, "Original GKG row lacks its registered publisher")
            url = row["https_probe_url"]
            urls = gkg_sources[source]["default_urls"]
            index = bisect.bisect_left(urls, url)
            require(index < len(urls) and urls[index] == url,
                    "Original GKG row URL lies outside its exact opportunity domain")
            themes = row["themes"] or [None]
            candidate = any(query_task(theme)["metadata_priority_hint"] == "mechanism_word_candidate"
                            for theme in themes)
            flags[source][index] |= 1 if candidate else 2
            row_counts["candidate_metadata_rows" if candidate else "ambiguous_metadata_rows"] += 1
            row_counts["valid_web_opportunity_rows"] += 1
            for theme in themes:
                task = query_task(theme)
                task_key = task["query_id"]
                task["matched_valid_web_rows"] += 1
                if theme is None:
                    task["matched_original_rows"] += 1
                _propose(task, seed, source, url)
    require(all(task["matched_original_rows"] >= task["matched_valid_web_rows"] for task in tasks.values()),
            "Original GKG theme inventory omitted a valid source row")
    selected, selected_urls, selected_keys = [], set(), set()

    def choose(source, url, role, query_id=None):
        if url in selected_urls:
            return False
        selected_urls.add(url); selected_keys.add((source, url))
        selected.append({"source_id": source, "url": url, "retrieval_role": role,
                         "query_id": query_id, "original_article_verified": False})
        return True

    # All real current official entry protocols are initial tasks; this is
    # distinct from claiming that their home pages contain qualified news.
    for source, rule in sorted(official.items()):
        for url in rule["default_urls"]:
            choose(source, url, "official_entrance_or_pending_protocol")
    official_requests = len(selected)
    selected_by_source, selected_by_query = Counter(), Counter()
    lanes = [sorted((task for task in tasks.values() if
              (task["metadata_priority_hint"] == "mechanism_word_candidate") == candidate),
              key=lambda task: _rank(seed, task["query_id"])) for candidate in (True, False)]
    # Interleave unresolved topics with mechanism hints. A large number of
    # machine economic tags cannot starve ambiguous emerging topics.
    ordered = [task for pair in zip_longest(*lanes) for task in pair if task is not None]
    remaining = GKG_SOURCE_REQUEST_BUDGET
    while remaining:
        progress = False
        for task in ordered:
            available = [row for row in task["proposals"].values() if row["url"] not in selected_urls]
            if not available:
                continue
            row = min(available, key=lambda row: (selected_by_source[row["source_id"]],
                      task["source_ranks"][row["source_id"]]))
            if choose(row["source_id"], row["url"], "unverified_metadata_query_candidate", task["query_id"]):
                selected_by_source[row["source_id"]] += 1
                selected_by_query[task["query_id"]] += 1
                remaining -= 1; progress = True
            if not remaining:
                break
        if not progress:
            break
    query_rows = [{key: value for key, value in task.items() if key not in {
                    "proposals", "source_ranks", "source_heap", "proposal_url_ranks", "proposal_sequence"}}
                  | {"proposed_source_count": len(task["proposals"]),
                     "scheduled_probe_urls": selected_by_query[task["query_id"]],
                     "status": "originals_unread_query_pending"}
                  for task in sorted(tasks.values(), key=lambda task: task["query_id"])]
    original_count = sum(len(set(rule["default_urls"])) for rule in sources.values())
    candidate_urls = sum(bool(flag & 1) for items in flags.values() for flag in items)
    ambiguous_urls = sum(not flag & 1 for items in flags.values() for flag in items)
    gkg_url_count = sum(len(rule["default_urls"]) for rule in gkg_sources.values())
    require(all(flag for items in flags.values() for flag in items),
            "Original GKG registry includes an opportunity without its source metadata row")
    require(candidate_urls+ambiguous_urls == gkg_url_count,
            "Every original GKG probe must retain a routing state")
    # Exact task duplicates are consolidated only for transport. Other source
    # adapters remain in the complement; one fetch does not certify an alias.
    selected_objects = [{"source_id": source, "url": url} for source, url in sorted(selected_keys)]
    registry_hash = fingerprint(registry)
    complement = {"schema_id": "registry_news_seed_complement_v1",
        "source_registry_ref": publisher["source_registry_ref"], "source_registry_hash": registry_hash,
        "selected_exact_keys": selected_objects, "unselected_protocol_url_count": original_count-len(selected_keys),
        "scope": "all_original_news_default_urls_minus_selected_exact_protocol_URL_keys",
        "interpretation": "not_selected_by_this_engineering_plan_not_no_economic_effect"}
    domain = {"schema_id": "original_publisher_GKG_opportunity_domain_v1",
        "publisher_discovery_id": publisher["discovery_id"], "source_registry_ref": publisher["source_registry_ref"],
        "source_registry_hash": registry_hash, "publisher_coverage": publisher["coverage"],
        "gdelt_discovery_id": gdelt["discovery_id"] if gdelt else None,
        "gdelt_coverage": publisher.get("gdelt_coverage"), "parsed_parts": parsed_parts,
        "metadata_file_states": file_states, "window_files_ref": emit("window_files_ref", gdelt["window_files"] if gdelt else []),
        "records_ref": gdelt["records_ref"] if gdelt else None,
        "excluded_records_ref": gdelt["excluded_records_ref"] if gdelt else None,
        "scope": "complete_original_known_opportunity_domain_with_failed_unparsed_files_preserved_global_denominator_unknown"}
    batches = []
    for source in dict.fromkeys(row["source_id"] for row in selected):
        urls = [row["url"] for row in selected if row["source_id"] == source]
        batches.extend({"source_id": source, "urls": urls[start:start+URLS_PER_BATCH]}
                       for start in range(0, len(urls), URLS_PER_BATCH))
    count = {**row_counts, "original_protocol_url_count": original_count,
        "original_GKG_probe_url_count": gkg_url_count, "candidate_metadata_probe_urls": candidate_urls,
        "ambiguous_metadata_probe_urls": ambiguous_urls, "query_tasks": len(query_rows),
        "official_initial_probe_requests": official_requests, "GKG_scheduled_probe_requests": len(selected)-official_requests,
        "selected_protocol_urls": len(selected_keys), "unselected_protocol_urls": original_count-len(selected_keys),
        "unfetched_original_protocol_urls": original_count, "captured_original_articles": 0,
        "unread_selected_probe_urls": len(selected), "queried_sources": len(selected_by_source),
        "unexamined_metadata_files": sum(row["status"] != "captured" for row in file_states)}
    coverage = {"publisher_coverage": publisher["coverage"], "GKG_metadata_coverage": publisher.get("gdelt_coverage"),
        "original_known_protocol_url_denominator": original_count, "world_news_denominator": None,
        "economic_relevant_news_denominator": None, "original_article_coverage": "none_no_fetch_performed",
        "retrieval_precision": None, "retrieval_recall": None, "news_time_window_exhaustiveness_proven": False}
    domain_ref = emit("complete_opportunities_ref", domain)
    source_budget = official_requests*MAX_PRODUCER_ATTEMPTS+GKG_SOURCE_REQUEST_BUDGET
    return {"schema_version": 1, "retrieval_id": operation_id, "workflow_id": payload["workflow_id"],
        "publisher_discovery_id": payload["publisher_discovery_id"], "request": payload, "request_hash": fingerprint(payload),
        "policy_id": POLICY_ID, "source_registry_ref": publisher["source_registry_ref"], "source_registry_hash": registry_hash,
        "gdelt_discovery_id": gdelt["discovery_id"] if gdelt else None, "window": publisher.get("gdelt_window"),
        "complete_opportunities_ref": domain_ref, "original_domain_ref": domain_ref,
        "query_tasks_ref": emit("query_tasks_ref", query_rows), "selected_urls_ref": emit("selected_urls_ref", selected),
        "unselected_urls_ref": emit("unselected_urls_ref", complement), "selection_batches": batches,
        "GKG_source_ids_ref": emit("GKG_source_ids_ref", sorted(gkg_sources)),
        "counts": count, "coverage": coverage,
        "resource_plan": {"policy_id": POLICY_ID, "GKG_source_request_budget": GKG_SOURCE_REQUEST_BUDGET,
            "official_initial_request_budget": official_requests, "planned_initial_source_requests": len(selected),
            "max_gkg_source_requests": GKG_SOURCE_REQUEST_BUDGET,
            "max_official_source_requests": official_requests*MAX_PRODUCER_ATTEMPTS,
            "max_source_requests": source_budget,
            "max_total_bytes": source_budget*2*1024*1024,
            "max_total_elapsed_seconds": source_budget*8,
            "basis": "versioned_response_time_and_memory_engineering_allowance_not_optimal_news_count_or_coverage_limit",
            "proposal_memory_per_query_sources": GKG_SOURCE_REQUEST_BUDGET,
            "shard_url_budget": URLS_PER_BATCH,
            "budget_scope": "all_initial_and_source_derived_actual_source_attempts_including_failures_and_producer_retries"},
        "status": "retrieval_plan_prepared_scope_pending", "original_news_fact_qualification": False,
        "historical_PIT_qualification": False, "trade_ready": False, "all_news_coverage_claimed": False,
        "limitations": ["Machine topics and mechanism words only route unresolved source questions, never facts or return directions.",
            "192 is an engineering allowance, not a scientific optimum, semantic ranking or complete news domain.",
            "Every unselected URL and failed metadata file remains pending; no economic-effect absence is inferred.",
            "Only exact probe URL transport tasks are merged; path, query and original metadata rows remain unchanged.",
            "Read selected originals, verify mechanisms and expand substantive or contrary-evidence research; keep remaining scope explicit.",
            "No retrieval accuracy, global majority, original publication, historical availability or fund-selection qualification is established."]}


def prepare(payload, store, artifacts, operation_id):
    """Produce only CAS objects; the public operation owns its final proof."""
    payload = _request(payload)
    require(type(operation_id) is str and operation_id, "Stable retrieval operation identity required")
    require(operation_id == payload["workflow_id"]+":retrieval",
            "Retrieval operation identity belongs to another workflow")
    store.assert_owned()
    publisher, registry, gdelt = _inputs(payload, store, artifacts, False)
    return _build(payload, publisher, registry, gdelt, artifacts, operation_id,
                  lambda name, value: artifacts.put_json(value))


def validate(value, store, artifacts, *, payload=None, replay_originals=True):
    """Independently rebuild from accepted originals; never write during replay."""
    original = _request(value["request"])
    require(value["retrieval_id"] == original["workflow_id"]+":retrieval",
            "Retrieval result identity belongs to another workflow")
    require(payload is None or original == _request(payload), "Retrieval differs from the public operation payload")
    require(value["request_hash"] == fingerprint(original), "Retrieval frozen request hash differs")
    publisher, registry, gdelt = _inputs(original, store, artifacts, replay_originals)

    def check(name, expected):
        reference = value.get(name)
        if name == "window_files_ref":
            reference = artifacts.read_json(value["complete_opportunities_ref"])[name]
        require(reference is not None and artifacts.read_json(reference) == expected,
                "Retrieval CAS differs from independently reconstructed originals: "+name)
        return reference

    rebuilt = _build(original, publisher, registry, gdelt, artifacts, value["retrieval_id"], check)
    require(value == rebuilt, "Retrieval result differs from independently rebuilt original opportunity routing")
    return {"status": "source_bound_retrieval_plan_checked", "original_news_fact_qualification": False,
            "historical_PIT_qualification": False, "trade_ready": False,
            "original_opportunity_scope_preserved": True, "replay_originals": False,
            "publisher_validation_basis": "independently_accepted_source_and_current_CAS_SHA256"}
