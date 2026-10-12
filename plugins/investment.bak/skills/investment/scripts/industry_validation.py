"""Independent algebra for source metric encodings, EN optima and PIT labels."""
import datetime as dt
import math
from contracts import fingerprint, instant
from numeric_validation import check, close, result


def expanded(model, row):
    from industry_model import FEATURE_NAMES, METRIC_FEATURE_NAMES
    encoder = model["feature_encoder"]
    from asset_domains import INPUT_SCHEMA_ID
    check(encoder.get("input_schema_id") == model.get("input_schema_id") == INPUT_SCHEMA_ID, "current input schema requires retrained model")
    check(encoder["base_feature_names"] == FEATURE_NAMES, "industry base economic schema")
    groups = {}
    for observation in row["feature_source"]["economic_features"]["observations"]:
        current = observation["current"]
        descriptor = {"category": observation["category"], "metric_id": observation["metric_id"],
            "source_concept": observation["source_concept"]["canonical_label"], "period_type": observation["source_concept"]["period_type"],
            "canonical_unit": current["unit"] if current is not None else "source_policy_action",
            "measurement_type": current["measurement_type"] if current is not None else "source_action"}
        groups.setdefault(fingerprint(descriptor), []).append(observation)
    vocabulary = encoder["vocabulary"]
    check(not set(groups)-{item["key"] for item in vocabulary}, "no silent unknown metric encoding")
    vector = list(row["x"])
    for item in vocabulary:
        bucket = groups.get(item["key"], [])
        actual = [float(o["current"]["value"]) for o in bucket if o["current"] is not None]
        delta = [float(o["current"]["value"])-float(o["prior"]["value"]) for o in bucket if o["current"] is not None and o["prior"] is not None]
        surprise = [float(o["current"]["value"])-float(o["expectation"]["value"]) for o in bucket if o["current"] is not None and o["expectation"] is not None]
        relative = [o["relative_delta"] for o in bucket if o["relative_delta"] is not None]
        rel_surprise = [o["relative_surprise"] for o in bucket if o["relative_surprise"] is not None]
        vector.append(len(bucket))
        for values in (actual, delta, surprise, relative, rel_surprise):
            vector.extend((math.fsum(values)/len(values) if values else 0., 1-len(values)/len(bucket) if bucket else 1.))
        for action in ("up", "down", "unchanged", "introduced", "removed"):
            vector.append(sum(bool(o["source_action"] and o["source_action"]["value"] == action) for o in bucket))
    names = FEATURE_NAMES+["source_metric:"+item["key"]+":"+name for item in vocabulary for name in METRIC_FEATURE_NAMES]
    check(names == model["feature_names"] == encoder["feature_names"] and len(vector) == len(names), "actual metric feature shape")
    return vector


def prediction(model, row):
    vector = expanded(model, row)
    return [float(intercept)+math.fsum(coef*(value-mean)/scale for coef,value,mean,scale in
        zip(coefs,vector,model["scaler_mean"],model["scaler_scale"])) for intercept,coefs in zip(model["intercept"],model["coefficients"])]


def model_math(model, train, names):
    from industry_model import fit_feature_encoder
    check(train and model["feature_encoder"] == fit_feature_encoder(train), "past-only metric vocabulary")
    check(model["source_metric_vocabulary_hash"] == fingerprint(model["feature_encoder"]["vocabulary"]), "metric vocabulary hash")
    vectors = [expanded(model,row) for row in train]
    width = len(model["feature_names"])
    check(len(model["coefficients"]) == len(model["intercept"]) == 2 and all(len(row) == width for row in model["coefficients"]), "industry model shape")
    for column in range(width):
        values = [vector[column] for vector in vectors]
        mean = math.fsum(values)/len(values)
        variance = math.fsum((value-mean)**2 for value in values)/len(values)
        bound = len(values)*math.ulp(1.)*variance+(len(values)*mean*math.ulp(1.))**2
        close(model["scaler_mean"][column],mean,"independent industry scaler mean")
        close(model["scaler_scale"][column],1 if variance <= bound else math.sqrt(variance),"independent industry scaler scale")
        check(model["scaler_scale"][column] > 0,"positive industry scale")
    z = [[(value-mean)/scale for value,mean,scale in zip(vector,model["scaler_mean"],model["scaler_scale"])] for vector in vectors]
    for target in range(2):
        beta,intercept = model["coefficients"][target],model["intercept"][target]
        residual = [row["targets"][target]-intercept-math.fsum(c*v for c,v in zip(beta,vector)) for row,vector in zip(train,z)]
        tolerance = 2e-6*max(1.,abs(model["alpha"]),max(abs(value) for value in residual))
        check(abs(math.fsum(residual)/len(train)) <= tolerance,"EN intercept stationarity")
        for column,coefficient in enumerate(beta):
            gradient = math.fsum(vector[column]*error for vector,error in zip(z,residual))/len(train)
            smooth = gradient-model["alpha"]*(1-model["l1_ratio"])*coefficient
            threshold = model["alpha"]*model["l1_ratio"]
            check(abs(smooth-threshold*math.copysign(1.,coefficient)) <= tolerance if coefficient else abs(smooth) <= threshold+tolerance,"independent EN KKT condition")
        if all(row["targets"][target] == train[0]["targets"][target] for row in train):
            check(all(value == 0 for value in beta),"constant target no forced coefficient")
            close(intercept,train[0]["targets"][target],"constant target intercept")


def validate_bridge_witness(witness, exposure, predicted_sectors):
    """Independent role vector and source snapshot arithmetic, no producer call."""
    from asset_domains import INPUT_SCHEMA_ID, ROLES
    from industry_model import FUND_FEATURE_NAMES
    check(witness.get("input_schema_id") == INPUT_SCHEMA_ID and witness["feature_names"] == FUND_FEATURE_NAMES,
          "current asset bridge schema")
    at, day = instant(witness["origin_at"]), witness["decision_date"]
    known = [row for row in exposure if row["as_of_date"] <= day and instant(row["known_at"]) <= at]
    selected = []
    for basis in ("historical_disclosure", "model_estimate"):
        group = [row for row in known if row["basis"] == basis]
        if group:
            latest = max((row["as_of_date"], row["known_at"]) for row in group)
            selected.extend(row for row in group if (row["as_of_date"], row["known_at"]) == latest)
    measured = {row["sector_id"] for row in selected if row["basis"] == "historical_disclosure"}
    selected = [row for row in selected if row["basis"] != "model_estimate" or row["sector_id"] not in measured]
    check(sorted(map(fingerprint, selected)) == sorted(map(fingerprint, witness["exposures"])) and selected,
          "independent disclosure and estimate snapshot selection")
    identities = [(row["sector_id"], row["role"], row.get("source_label"), row["basis"]) for row in selected]
    check(len(set(identities)) == len(identities), "no duplicate source exposure")
    role_values = {role: [0., 0., 0.] for role in ROLES}
    capital, stale, hashes = {}, {}, {}
    for row in selected:
        role, basis = row["role"], row["basis"]
        check(role in role_values and row.get("evidence_refs"), "source role and exposure evidence")
        coefficient = float(row["coefficient"] if basis == "model_estimate" else row["weight"])
        check(math.isfinite(coefficient) and (basis == "model_estimate" or 0 < coefficient <= 1),
              "signed estimated beta versus measured capital weight")
        forecast = predicted_sectors[row["sector_id"]]
        check(forecast.get("input_schema_id") == INPUT_SCHEMA_ID and forecast["decision_date"] == day,
              "same-origin source asset forecast")
        check(forecast["sector_id"] == row["sector_id"] and len(forecast["predicted_coordinates"]) == 2,
              "source asset prediction identity and coordinates")
        for column in range(2):
            role_values[role][column] += coefficient*forecast["predicted_coordinates"][column]
        role_values[role][2] = 1.
        hashes[row["sector_id"]] = forecast["source_hash"]
        if basis == "historical_disclosure":
            group = capital
            label = row["source_label"]
            check(label not in group or group[label] == coefficient, "overlap preserves one original capital weight")
            group[label] = coefficient
            if row["as_of_date"] < day:
                stale[label] = coefficient
    unknown = 1.
    reference_unmapped = 1-math.fsum(capital.values())
    stale_share = math.fsum(stale.values())
    expected = [value for role in ROLES for value in role_values[role]]+[unknown, stale_share,
                float(any(row["basis"] == "model_estimate" for row in selected))]
    check(hashes == witness["sector_prediction_hashes"] and len(witness["values"]) == len(FUND_FEATURE_NAMES),
          "asset bridge forecast hash and feature shape")
    for role in ROLES:
        for actual, value in zip(witness["role_values"][role], role_values[role]):
            close(actual,value,"independent role vector arithmetic")
    for actual, value in zip(witness["values"],expected):
        close(actual,value,"independent fund asset covariate")
    close(witness["disclosed_reference_unmapped_weight"],reference_unmapped,"unique disclosed reference capital weight")
    close(witness["actual_current_unknown_weight"],unknown,"estimated/stale exposure never removes current unknown capital")
    check(witness["source_hash"] == fingerprint({key:value for key,value in witness.items() if key != "source_hash"}),
          "asset bridge witness hash")
    return {"status": "passed", "scope": "role_covariates_and_current_unknown_capital_not_asset_wealth_addition"}


def validate(output, inputs):
    from industry_model import FEATURE_NAMES,TARGET_NAMES,build_samples
    data,origins,policy = inputs["data"],inputs["origins"],inputs["policy"]
    time = inputs["clock"]["order_time_local"]
    source_rows,_ = build_samples(data,origins,inputs["context"]["model_request"]["horizon_days"],time)
    check(output["samples"] == source_rows and output["dataset_hash"] == fingerprint(data),"source/PIT sample reconstruction")
    sectors = {row["sector_id"]:row for row in data["sectors"]}
    check(output["sector_ids"] == sorted(sectors) and output["news_ref"] == data["news_ref"] and output["event_frontier_hash"] == data["news_state"]["event_frontier_hash"],"source identity and frontier")
    check(output["forecast_hash"] == fingerprint({k:v for k,v in output.items() if k != "forecast_hash"}),"industry content hash")
    rows = {(row["sector_id"],row["decision_date"]):row for row in source_rows}
    for row in source_rows:
        at = instant(row["feature_source"]["origin_at"])
        check(instant(row["feature_source"]["benchmark_base"]["available_at"]) <= at,"PIT price feature")
        for obs in row["feature_source"]["economic_features"]["observations"]:
            check(instant(obs["known_at"]) <= at and instant(obs["assessed_at"]) <= at,"source known and assessed economic input")
        source_prices = sectors[row["sector_id"]].get("price_versions",sectors[row["sector_id"]]["prices"])
        for label in row["label_versions"]:
            source = label["label_source"]
            check(source["phase"] in source_prices and source["terminal"] in source_prices,"measured label quote versions")
            check(source["phase"]["date"] > source["source_decision_date"] and source["terminal"]["date"] >= source["target_not_before"],"future measured label phases")
            for column,key in enumerate(("phase","terminal")):
                target = source["asset_domain"]["return_target"]
                value, base = float(source[key]["close"]), float(source["base"]["close"])
                expected = math.log(value/base) if target["transform"] == "price_log_return" else value-base
                close(label["targets"][column],expected,"independent source target transformation")
            check(label["label_available_at"] == max((source["phase"]["available_at"],source["terminal"]["available_at"]),key=instant),"actual label maturity")
    def eligible(sector,day,ceiling=None,inventory=None):
        boundary = instant(day+"T"+time+"+08:00")
        if ceiling is not None: boundary=min(boundary,instant(ceiling))
        lower=(dt.date.fromisoformat(day)-dt.timedelta(days=policy["train_window_days"])).isoformat()
        selected=[]
        for row in source_rows if inventory is None else inventory:
            if row["sector_id"] != sector or not lower <= row["decision_date"] < day: continue
            versions=[v for v in row["label_versions"] if instant(v["label_available_at"]) < boundary]
            if not versions: continue
            value=dict(row); value.update(max(versions,key=lambda v:instant(v["label_available_at"]))); value["label_versions"]=versions
            selected.append(value)
        return sorted(selected,key=lambda row:row["decision_date"])
    for forecast in output["forecasts"]:
        sector,day=forecast["sector_id"],forecast["decision_date"]
        ceiling=output.get("training_ceiling_at") if day == output["decision_date"] else None
        train,audit=eligible(sector,day,ceiling),forecast["training_audit"]
        check(audit["training_data_hash"] == fingerprint(train) and audit["training_rows"] == len(train) and audit["policy_hash"] == fingerprint(policy) and audit.get("training_ceiling_at") == ceiling,"causal industry fit binding")
        check(audit["max_label_available_at"] == max((row["label_available_at"] for row in train),key=instant),"source training maturity")
        model=forecast["model"]
        check(model["target_names"] == TARGET_NAMES and forecast["feature_source"] == rows[(sector,day)]["feature_source"],"source feature/target binding")
        model_math(model,train,FEATURE_NAMES)
        for actual,expected in zip(forecast["predicted_coordinates"],prediction(model,rows[(sector,day)])): close(actual,expected,"independent industry dot product")
        check(forecast["source_hash"] == fingerprint({k:v for k,v in forecast.items() if k != "source_hash"}),"industry forecast hash")
        initial=max(policy["min_train_dates"],int(len(train)*policy["cv_initial_train_fraction"]))
        cuts=[initial+(len(train)-initial)*i//policy["cv_folds"] for i in range(policy["cv_folds"]+1)]
        folds=[(eligible(sector,train[start]["decision_date"],inventory=train),train[start:stop]) for start,stop in zip(cuts[:-1],cuts[1:])]
        check(len(audit["folds"]) == len(folds),"CV inventory")
        for declared,(mature,validation) in zip(audit["folds"],folds):
            check(len(mature) >= policy["min_train_dates"] and declared["training_data_hash"] == fingerprint(mature) and declared["validation_start"] == validation[0]["decision_date"] and declared["validation_end"] == validation[-1]["decision_date"],"inner CV source boundary")
        for trial in audit["cv_trials"]:
            check(len(trial["fold_models"]) == len(trial["fold_mse"]) == len(folds),"CV model witnesses")
            for declared,fitted,(mature,validation) in zip(trial["fold_mse"],trial["fold_models"],folds):
                model_math(fitted,mature,FEATURE_NAMES)
                error=math.fsum(math.fsum((a-p)**2 for a,p in zip(row["targets"],prediction(fitted,row)))/2 for row in validation)/len(validation)
                close(declared,error,"independent CV MSE")
            close(trial["mean_mse"],math.fsum(trial["fold_mse"])/len(folds),"CV average")
        best=min(audit["cv_trials"],key=lambda t:(t["mean_mse"],t["alpha"],t["l1_ratio"]))
        check((model["alpha"],model["l1_ratio"]) == (best["alpha"],best["l1_ratio"]),"chronological parameter choice")
    latest=max(origins) if origins else None
    ready_ids=sorted(sector for sector in sectors if any(row["sector_id"] == sector and row["decision_date"] == latest for row in output["forecasts"]))
    check(output["ready_sector_ids"] == ready_ids,"local industry readiness")
    ready=bool(sectors) and len(ready_ids) == len(sectors)
    check(output["trade_ready"] == ready and output["status"] == ("research_ready" if ready else "insufficient_evidence"),"industry scope readiness")
    return result(["source_vintage_PIT_samples","independent_encoder_scaler_EN_KKT_CV"],output["status"],reason=output.get("reason"))
