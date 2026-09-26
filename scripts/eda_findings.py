"""
Reproducible EDA: workflow selection evidence and label validity checks.
Factored AI & Data Hackathon 2026

Usage (from the repository root, which contains data/silver):
    pip install duckdb scikit-learn pandas pyarrow
    python eda_findings.py

Outputs: reports/eda/*.csv and reports/eda/label_signal.json
Fixed temporal split: train < 2025-07-01 <= test. Fixed seed.
"""
import json
from pathlib import Path

import duckdb
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, roc_auc_score

SEED = 42
CUTOFF = "2025-07-01"

base = Path.cwd()
silver = next(p for p in [base / "data" / "silver", base / "silver", base] if (p / "call_center_interactions.parquet").exists())
out = base / "reports" / "eda"
out.mkdir(parents=True, exist_ok=True)

con = duckdb.connect()
for t in ["call_center_interactions", "call_transcripts", "complaints", "service_agents"]:
    con.execute(f"CREATE VIEW {t} AS SELECT * FROM '{(silver / f'{t}.parquet').as_posix()}'")


def save(name: str, sql: str):
    df = con.sql(sql).df()
    df.to_csv(out / f"{name}.csv", index=False)
    print(f"\n== {name}\n{df.to_string(index=False)}")
    return df


# 1. Demand & operational workload by contact reason
save("workload_by_reason", """
    SELECT reason_category,
           count(*)                                            AS interactions,
           round(100.0 * count(*) / sum(count(*)) OVER (), 1)  AS pct_volume,
           round(avg(duration_seconds))                        AS avg_handle_sec,
           round(100.0 * sum(duration_seconds) / sum(sum(duration_seconds)) OVER (), 1) AS pct_agent_time,
           round(100 * avg(was_resolved::INT), 1)              AS fcr_pct,
           count(*) FILTER (WHERE NOT was_resolved)            AS unresolved,
           round(100.0 * count(*) FILTER (WHERE NOT was_resolved)
                 / sum(count(*) FILTER (WHERE NOT was_resolved)) OVER (), 1) AS pct_of_all_unresolved,
           round(100 * avg(was_escalated::INT), 1)             AS escalated_pct
    FROM call_center_interactions GROUP BY 1 ORDER BY interactions DESC
""")

save("complaints_by_subcategory", """
    SELECT category, coalesce(subcategory, '(null)') AS subcategory, count(*) AS n,
           round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct,
           round(100 * avg((status = 'Escalated')::INT), 1)  AS escalated_pct,
           round(avg(claimed_amount))                         AS avg_claimed
    FROM complaints GROUP BY 1, 2 ORDER BY n DESC
""")

# 2. Label validity
for col in ["detected_sentiment", "channel", "customer_detected_accent", "requires_followup"]:
    save(f"escalation_by_{col}", f"""
        SELECT {col}, count(*) n, round(100 * avg(was_escalated::INT), 2) AS escalated_pct
        FROM call_center_interactions GROUP BY 1 ORDER BY n DESC
    """)

# 3. Text diversity on transcripts and complaint descriptions
save("transcript_diversity", """
    SELECT count(*) AS transcripts, count(DISTINCT full_text) AS distinct_full_text,
           count(DISTINCT customer_text) AS distinct_customer_text,
           count(DISTINCT detected_intents) AS distinct_intents,
           round(100 * avg((full_text LIKE '%{%}%')::INT), 1) AS pct_unfilled_placeholders
    FROM call_transcripts
""")
save("complaint_text_diversity", """
    SELECT count(*) AS complaints, count(DISTINCT description) AS distinct_descriptions,
           count(origin_interaction_id) AS with_origin_interaction
    FROM complaints
""")

# 4. Learnable signal (baselines with a temporal split)
results = {}
j = con.sql("""
    SELECT t.full_text, i.* FROM call_transcripts t JOIN call_center_interactions i USING (interaction_id)
""").df()
tr, te = j[j.interaction_date < CUTOFF], j[j.interaction_date >= CUTOFF]
vec = TfidfVectorizer(ngram_range=(1, 2), min_df=2)
Xtr, Xte = vec.fit_transform(tr.full_text), vec.transform(te.full_text)
for tgt in ["was_escalated", "was_resolved"]:
    m = LogisticRegression(max_iter=2000, class_weight="balanced", random_state=SEED).fit(Xtr, tr[tgt])
    results[f"text_tfidf_lr__{tgt}__auc"] = round(roc_auc_score(te[tgt], m.predict_proba(Xte)[:, 1]), 3)
m = LogisticRegression(max_iter=2000, random_state=SEED).fit(Xtr, tr.reason_category)
pred = m.predict(Xte)
results["text_tfidf_lr__reason_category__acc"] = round(float(np.mean(pred == te.reason_category)), 3)
results["text_tfidf_lr__reason_category__majority_acc"] = round(float(np.mean(te.reason_category == tr.reason_category.mode()[0])), 3)
results["text_tfidf_lr__reason_category__macro_f1"] = round(f1_score(te.reason_category, pred, average="macro"), 3)

i = con.sql("SELECT * FROM call_center_interactions").df()
X = i[["duration_seconds", "wait_time_seconds", "sentiment_score"]].copy()
for k in ["interaction_type", "channel", "reason_category", "detected_sentiment", "customer_detected_accent"]:
    X[k] = i[k].astype("category").cat.codes
train_mask = i.interaction_date < CUTOFF
for tgt in ["was_escalated", "was_resolved"]:
    g = HistGradientBoostingClassifier(max_iter=200, random_state=SEED).fit(X[train_mask], i[tgt][train_mask])
    results[f"structured_gbm__{tgt}__auc"] = round(roc_auc_score(i[tgt][~train_mask], g.predict_proba(X[~train_mask])[:, 1]), 3)

results["split"] = {"cutoff": CUTOFF, "seed": SEED, "train_rows_text": len(tr), "test_rows_text": len(te)}
(out / "label_signal.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print("\n== label_signal\n" + json.dumps(results, indent=2))
