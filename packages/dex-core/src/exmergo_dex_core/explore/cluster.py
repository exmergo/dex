"""Explore: k-means clustering over a bounded numeric feature sample.

Discovery, not exfiltration (Principle 2). A bounded sample of numeric,
non-PII feature columns is pulled into the engine process, standardized, and
clustered with scikit-learn in memory. Only aggregates ever cross the stdout
boundary: cluster sizes and centroids, where a centroid coordinate is the mean
of that cluster's feature values, exactly the measuring aggregate the query
firewall already treats as safe. Raw sampled rows live and die inside this
module; they never reach an :class:`~..envelope.Envelope`.

Two levers keep a metered warehouse cheap: the sample query selects only the
feature columns (columnar warehouses bill by columns scanned) and carries a
dialect-aware sample clause (TABLESAMPLE / SAMPLE / USING SAMPLE) so a fraction
is read, not the whole table. Both are visible to the cost gate, which prices
the sample query with a free dry-run before anything runs.

scikit-learn is an optional dependency behind the ``[cluster]`` extra, imported
lazily so the light default install never pulls numpy/scipy. A missing extra
surfaces as an actionable :class:`ClusterDependencyError`, not an import crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

# Percent-sampling dialects need a floor so a tiny fraction on a huge table does
# not round to zero percent (which reads the whole table on some engines).
_MIN_SAMPLE_PERCENT = 0.01

# Below this share of the sample, a cluster is an outlier pocket rather than a
# segment. Engine-fixed, not configurable: it decides only what the notes say,
# never what the clustering does, so there is nothing here for a caller to tune.
_DEGENERATE_CLUSTER_FRACTION = 0.01


class ClusterError(Exception):
    """A clustering request that cannot be satisfied (too few rows, too few
    usable features, k out of range). Surfaced as a clean error envelope."""


class ClusterDependencyError(Exception):
    """scikit-learn (the ``[cluster]`` extra) is not installed."""


# Dialects whose sample clause accepts a seed, each verified against the engine
# itself rather than assumed from its docs. Everything else re-draws per run, and
# `sample_is_repeatable` is what makes the envelope say so out loud: a seed knob
# that silently does nothing on four of six connectors would be a lie, the same
# reason `references/redshift.md` refuses to ship a sampling threshold there.
_SEEDABLE_DIALECTS = frozenset({"duckdb"})


def sample_is_repeatable(dialect: str, seed: int | None) -> bool:
    """Whether this dialect's sample clause draws the same rows twice."""

    return seed is not None and dialect in _SEEDABLE_DIALECTS


def _sample_parts(
    dialect: str, sample_rows: int, row_count: int | None, seed: int | None = None
) -> tuple[str, str, str]:
    """The dialect-specific sampling pieces: a suffix attached to the table in
    the FROM, a tail appended after WHERE, and a human note naming the method.

    Most engines attach the sample to the table (``TABLESAMPLE`` / ``SAMPLE``);
    DuckDB samples the query result with a trailing ``USING SAMPLE``; Redshift
    has no sampling construct, so it draws a random-ordered top-N. Percent
    engines size the fraction from the cached row count; an unknown or
    already-small table skips sampling and leans on column pruning plus the cost
    gate (the fetch is capped either way).
    """

    n = int(sample_rows)

    def percent() -> float | None:
        if not row_count or row_count <= n:
            return None
        return max(_MIN_SAMPLE_PERCENT, round(100.0 * n / row_count, 4))

    if dialect == "duckdb":
        if seed is not None:
            # The seeded form needs the explicit reservoir(...) spelling; DuckDB
            # rejects REPEATABLE on the bare `USING SAMPLE n ROWS`.
            return (
                "",
                f" USING SAMPLE reservoir({n} ROWS) REPEATABLE ({seed})",
                f"USING SAMPLE {n} ROWS (reservoir, repeatable seed {seed})",
            )
        return "", f" USING SAMPLE {n} ROWS", f"USING SAMPLE {n} ROWS (reservoir)"
    if dialect == "snowflake":
        return f" SAMPLE ({n} ROWS)", "", f"SAMPLE ({n} ROWS)"
    if dialect == "databricks" or dialect == "spark":
        return f" TABLESAMPLE ({n} ROWS)", "", f"TABLESAMPLE ({n} ROWS)"
    if dialect == "bigquery":
        p = percent()
        if p is None:
            return "", "", "no sample clause (row count unknown or below cap)"
        return f" TABLESAMPLE SYSTEM ({p} PERCENT)", "", f"TABLESAMPLE SYSTEM ({p}%)"
    if dialect == "postgres":
        p = percent()
        if p is None:
            return "", "", "no sample clause (row count unknown or below cap)"
        return f" TABLESAMPLE SYSTEM ({p})", "", f"TABLESAMPLE SYSTEM ({p}%)"
    if dialect == "redshift":
        # No TABLESAMPLE on Redshift: a random-ordered top-N is the portable
        # sample. It is a full scan bounded by the budget, so the note says so.
        return "", f" ORDER BY RANDOM() LIMIT {n}", f"ORDER BY RANDOM() LIMIT {n}"
    # Unknown dialect: no sampling clause. The row-cap on the fetch and the cost
    # gate still bound the work; column pruning still limits the scan.
    return "", "", "no sample clause (unrecognized dialect)"


def build_sample_sql(
    identifier: str,
    feature_names: list[str],
    *,
    dialect: str,
    sample_rows: int,
    row_count: int | None,
    seed: int | None = None,
) -> tuple[str, str]:
    """Build the read-only feature-sample SELECT and describe how it samples.

    sqlglot renders the identifiers (``identify=True`` quotes every part in the
    connector's own dialect, so a mixed-case or reserved-word name is safe) and
    the null filter; the sample clause is composed by position so it lands
    exactly where each engine expects it. Returns ``(sql, sample_method)``. The
    result is a single SELECT of only the feature columns, so it passes
    ``guards.sql_guard.assert_select_only`` and scans no column a feature does
    not name.
    """

    if not feature_names:
        raise ClusterError("no feature columns to sample")

    parts = identifier.split(".")
    if len(parts) == 3:
        table = exp.table_(parts[2], db=parts[1], catalog=parts[0])
    elif len(parts) == 2:
        table = exp.table_(parts[1], db=parts[0])
    else:
        table = exp.table_(parts[-1])

    def render(node: exp.Expression) -> str:
        return node.sql(dialect=dialect, identify=True)

    table_ref = render(table)
    cols_sql = ", ".join(render(exp.column(name)) for name in feature_names)
    where_sql = " AND ".join(
        f"{render(exp.column(name))} IS NOT NULL" for name in feature_names
    )

    table_suffix, tail, method = _sample_parts(dialect, sample_rows, row_count, seed)
    sql = f"SELECT {cols_sql} FROM {table_ref}{table_suffix} WHERE {where_sql}{tail}"  # noqa: S608
    return sql, method


@dataclass
class ClusterResult:
    """The clustering summary that becomes the envelope payload: aggregates
    only, no row ever survives here."""

    k: int
    k_selection: str  # "explicit" | "silhouette"
    features: list[str]
    standardized: bool
    n_samples: int
    dropped_null_rows: int
    silhouette: float | None
    inertia: float
    iterations: int
    converged: bool
    clusters: list[dict] = field(default_factory=list)
    k_sweep: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_data(self) -> dict:
        return {
            "k": self.k,
            "k_selection": self.k_selection,
            "features": self.features,
            "standardized": self.standardized,
            "n_samples": self.n_samples,
            "dropped_null_rows": self.dropped_null_rows,
            "silhouette": self.silhouette,
            "inertia": self.inertia,
            "iterations": self.iterations,
            "converged": self.converged,
            "clusters": self.clusters,
            "k_sweep": self.k_sweep,
            "notes": self.notes,
        }


def _sklearn():
    """Import scikit-learn lazily, translating a missing extra into a clean,
    actionable error rather than a bare ImportError from deep in a stack."""

    try:
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ClusterDependencyError(
            "k-means clustering needs scikit-learn, which is not installed. "
            "Reinstall the engine with the [cluster] extra "
            "(exmergo-dex-core[<connector>,cluster]); the explore skill installs "
            "it automatically when it sees an `explore cluster` command."
        ) from exc
    return np, KMeans, StandardScaler, silhouette_score


def ensure_available() -> None:
    """Raise :class:`ClusterDependencyError` if scikit-learn is not importable.

    Called before opening a connection or pricing a sample query, so a missing
    ``[cluster]`` extra costs nothing: no warehouse round-trip, no spend."""

    _sklearn()


def _clean_rows(cells: list[list]) -> tuple[list[list[float]], int]:
    """Coerce sampled cells to float rows, dropping any row with a null or a
    non-numeric cell. Returns the kept rows and the dropped count."""

    kept: list[list[float]] = []
    dropped = 0
    for row in cells:
        try:
            if any(v is None for v in row):
                dropped += 1
                continue
            kept.append([float(v) for v in row])
        except (TypeError, ValueError):
            dropped += 1
    return kept, dropped


def _round(value: float) -> float:
    """Round a centroid coordinate for display without pretending to precision
    the sample cannot support, while keeping small magnitudes readable."""

    return round(float(value), 6)


def cluster_features(
    feature_names: list[str],
    cells: list[list],
    *,
    k: int | None,
    k_min: int,
    k_max: int,
    silhouette_sample: int,
    random_state: int,
) -> ClusterResult:
    """Standardize the feature sample and run k-means, selecting k by silhouette
    when it was not given. Pure and connector-agnostic: it takes already-fetched
    cells and returns aggregates, so it is unit-testable without a warehouse."""

    np, KMeans, StandardScaler, silhouette_score = _sklearn()  # noqa: N806

    rows, dropped = _clean_rows(cells)
    n_samples = len(rows)
    if n_samples < max(k_min, 2):
        raise ClusterError(
            f"only {n_samples} complete row(s) after dropping nulls; too few to "
            "cluster (need a larger sample or fewer/less-sparse features)"
        )

    # Deterministic input order so a fixed random_state yields a reproducible
    # labeling (k-means++ seeding is index-sensitive).
    rows.sort()
    n_distinct = len({tuple(r) for r in rows})
    x = np.asarray(rows, dtype=float)

    scaler = StandardScaler()
    x_scaled = scaler.fit_transform(x)

    notes: list[str] = ["standardized features (z-score) before k-means"]

    k_ceiling = min(k_max, n_samples - 1, n_distinct)
    if k_ceiling < 2:
        raise ClusterError(
            f"the sample has only {n_distinct} distinct feature-vector(s); k-means "
            "needs at least 2 distinct points"
        )

    def _silhouette(labels) -> float | None:
        if len(set(labels)) < 2:
            return None
        try:
            size = min(silhouette_sample, len(labels))
            score = silhouette_score(
                x_scaled,
                labels,
                sample_size=size if size < len(labels) else None,
                random_state=random_state,
            )
            return round(float(score), 4)
        except ValueError:
            return None

    def _fit(n_clusters: int):
        model = KMeans(n_clusters=n_clusters, random_state=random_state, n_init=10)
        labels = model.fit_predict(x_scaled)
        return model, labels

    k_sweep: list[dict] = []
    if k is not None:
        if k < 2:
            raise ClusterError(f"k must be at least 2 (got {k})")
        if k > k_ceiling:
            raise ClusterError(
                f"k={k} exceeds what the sample supports (at most {k_ceiling}: "
                f"{n_distinct} distinct point(s), {n_samples} row(s))"
            )
        model, labels = _fit(k)
        k_selection = "explicit"
        silhouette = _silhouette(labels)
    else:
        best = None  # (silhouette, k, model, labels)
        for candidate in range(max(k_min, 2), k_ceiling + 1):
            model, labels = _fit(candidate)
            score = _silhouette(labels)
            k_sweep.append(
                {
                    "k": candidate,
                    "silhouette": score,
                    "inertia": round(float(model.inertia_), 4),
                }
            )
            if score is not None and (best is None or score > best[0]):
                best = (score, candidate, model, labels)
        if best is None:
            # No k produced a defined silhouette (e.g. every split degenerate):
            # fall back to the smallest k so the command still returns structure.
            model, labels = _fit(max(k_min, 2))
            silhouette = None
        else:
            silhouette, _, model, labels = best
        k = int(model.n_clusters)
        k_selection = "silhouette"
        if k_ceiling < k_max:
            notes.append(
                f"swept k in [{max(k_min, 2)}, {k_ceiling}] (capped by sample size "
                f"and distinct points; configured k_max={k_max})"
            )
        else:
            notes.append(
                f"selected k by best silhouette over [{max(k_min, 2)}, {k_ceiling}]"
            )

    centroids = scaler.inverse_transform(model.cluster_centers_)
    sizes = [int((labels == i).sum()) for i in range(k)]
    clusters = [
        {
            "label": i,
            "size": sizes[i],
            "fraction": round(sizes[i] / n_samples, 4),
            "centroid": {
                name: _round(centroids[i][j]) for j, name in enumerate(feature_names)
            },
        }
        for i in range(k)
    ]

    if silhouette is not None and silhouette_sample < n_samples:
        notes.append(
            f"silhouette computed on a {silhouette_sample}-row subsample of the "
            f"{n_samples}-row sample"
        )

    tiny = [c for c in clusters if c["fraction"] < _DEGENERATE_CLUSTER_FRACTION]
    if tiny and len(tiny) < k:
        worst = min(c["fraction"] for c in tiny)
        notes.append(
            f"{len(tiny)} of {k} cluster(s) hold under "
            f"{_DEGENERATE_CLUSTER_FRACTION:.0%} of the sample (smallest: "
            f"{worst:.2%}, {min(c['size'] for c in tiny)} row(s)); a cluster that "
            "small is an outlier pocket rather than a segment, and it inflates "
            "the silhouette. Read this as outlier detection, or re-run with -k "
            "to force a split of the bulk"
        )

    return ClusterResult(
        k=k,
        k_selection=k_selection,
        features=list(feature_names),
        standardized=True,
        n_samples=n_samples,
        dropped_null_rows=dropped,
        silhouette=silhouette,
        inertia=round(float(model.inertia_), 4),
        iterations=int(model.n_iter_),
        converged=bool(model.n_iter_ < model.max_iter),
        clusters=clusters,
        k_sweep=k_sweep,
        notes=notes,
    )
