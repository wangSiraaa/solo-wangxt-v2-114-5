import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const STATUS_BADGE = {
  candidate: "candidate",
  validated: "validated",
  approved: "approved",
  withdrawn: "withdrawn",
  complete: "approved",
  incomplete: "incomplete",
};

function num(v, d = 3) {
  return v == null ? "—" : Number(v).toFixed(d);
}
function mg(kg) { return kg == null ? "—" : (kg / 1000).toFixed(3); }
function pct(v) { return v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`; }

// default blank candidate row for the composer
function blankEq(code, version) {
  return { code, version, a: "", b: 2.4, c: 0.6, dbh_min_cm: 5,
           dbh_max_cm: 100, height_required: true, residual_sigma: 0.18,
           citation: "" };
}

export default function EquationReviewPanel() {
  const [species, setSpecies] = useState([]);
  const [versions, setVersions] = useState([]);
  const [reviews, setReviews] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [comparisons, setComparisons] = useState([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [composing, setComposing] = useState(false);

  async function refresh() {
    const [sp, vs, rs] = await Promise.all(
      [api.species(), api.estimates(), api.reviews()]);
    setSpecies(sp); setVersions(vs); setReviews(rs);
  }
  useEffect(() => { refresh().catch((e) => setErr(e.message)); }, []);
  useEffect(() => {
    if (openId == null) { setDetail(null); setComparisons([]); return; }
    Promise.all([api.review(openId), api.reviewComparisons(openId)])
      .then(([d, cs]) => { setDetail(d); setComparisons(cs); })
      .catch((e) => setErr(e.message));
  }, [openId]);

  const confirmed = useMemo(
    () => versions.filter((v) => v.status === "confirmed"), [versions]);

  async function run(fn) {
    setBusy(true); setErr("");
    try { await fn(); await refresh(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div>
      <h2>Equation adoption review</h2>
      <p className="hint">
        Propose a <strong>new</strong> allometric equation set, validate it,
        then compare it against a <strong>locked confirmed edition</strong>.
        The comparison re-reads that edition&apos;s survey data, human
        identity decisions and sampling-design snapshot and re-runs its
        equations; only if the confirmed result is reproduced byte-for-byte
        are candidate differences attributed to the equation alone. A
        complete comparison can be approved to mint an <em>independent new
        confirmed edition</em> — the baseline never changes. Candidates are
        never selectable by the ordinary two-occasion estimate flow.
      </p>
      {err && <div className="error">{err}</div>}

      <div className="review-toolbar">
        <button disabled={busy} onClick={() => setComposing((x) => !x)}>
          {composing ? "Close composer" : "+ New candidate"}
        </button>
      </div>

      {composing && (
        <CandidateComposer
          species={species} busy={busy}
          onCancel={() => setComposing(false)}
          onCreated={async (id) => { setComposing(false); setOpenId(id);
                                     await refresh(); }} />
      )}

      <section>
        <h3>Reviews (candidate → validated → approved / withdrawn)</h3>
        <table className="version-table review-table">
          <thead>
            <tr><th>#</th><th>label</th><th>species</th><th>status</th>
              <th>created</th><th>outcome</th></tr>
          </thead>
          <tbody>
            {reviews.map((r) => (
              <tr key={r.id} className={r.status}
                  onClick={() => setOpenId(r.id)}>
                <td>{r.id}</td>
                <td>{r.label}</td>
                <td>{r.species_scope.join(", ")}</td>
                <td><Badge value={r.status} /></td>
                <td>{new Date(r.created_at).toLocaleString()}</td>
                <td>{r.approved_version_id
                    ? `new edition #${r.approved_version_id}`
                    : r.status === "withdrawn" ? "withdrawn (audit kept)"
                    : `${r.events.filter((e) => e.event === "compared").length}
                       comparison(s)`}</td>
              </tr>
            ))}
            {reviews.length === 0 && (
              <tr><td colSpan="6" className="hint">No reviews yet.</td></tr>
            )}
          </tbody>
        </table>
      </section>

      {detail && (
        <ReviewDetail
          review={detail} comparisons={comparisons}
          confirmedVersions={confirmed} busy={busy}
          onAction={run}
          onReload={async () => {
            const [d, cs] = await Promise.all(
              [api.review(detail.id), api.reviewComparisons(detail.id)]);
            setDetail(d); setComparisons(cs);
          }} />
      )}
    </div>
  );
}

function Badge({ value }) {
  return <span className={`badge review-status-${STATUS_BADGE[value] || "candidate"}`}>
    {value}
  </span>;
}

// ------------------------------------------------------------- composer
function CandidateComposer({ species, busy, onCancel, onCreated }) {
  const [rows, setRows] = useState(() =>
    Object.fromEntries(species.map((s) =>
      [s.code, blankEq(`${s.code}-AGB`, "2.0")])));
  const [label, setLabel] = useState("candidate equations");
  const [localErr, setLocalErr] = useState("");

  function set(sp, field, val) {
    setRows((rs) => ({ ...rs, [sp]: { ...rs[sp], [field]: val } }));
  }

  async function submit() {
    setLocalErr("");
    const spec = {};
    for (const [sp, e] of Object.entries(rows)) {
      if (e.code === "" && e.a === "") continue; // skipped species
      spec[sp] = {
        ...e,
        a: parseFloat(e.a), b: parseFloat(e.b), c: parseFloat(e.c),
        dbh_min_cm: parseFloat(e.dbh_min_cm),
        dbh_max_cm: parseFloat(e.dbh_max_cm),
        residual_sigma: parseFloat(e.residual_sigma),
      };
      if (!spec[sp].citation.trim()) {
        setLocalErr(`${sp}: citation is required`); return;
      }
    }
    if (!Object.keys(spec).length) {
      setLocalErr("add at least one species equation"); return;
    }
    try {
      const r = await api.createReview({ label, candidate_spec: spec });
      onCreated(r.id);
    } catch (e) { setLocalErr(e.message); }
  }

  return (
    <section className="composer">
      <h3>New candidate equation set</h3>
      {localErr && <div className="error">{localErr}</div>}
      <label>Review label <input value={label}
        onChange={(e) => setLabel(e.target.value)} /></label>
      <table className="candidate-table">
        <thead>
          <tr><th>species</th><th>code</th><th>version</th>
            <th>a</th><th>b</th><th>c</th>
            <th>dbh min</th><th>dbh max</th><th>σ(ln AGB)</th>
            <th>citation</th></tr>
        </thead>
        <tbody>
          {species.map((s) => {
            const e = rows[s.code] || blankEq();
            return (
              <tr key={s.code}>
                <td><strong>{s.code}</strong></td>
                <td><input value={e.code}
                    onChange={(ev) => set(s.code, "code", ev.target.value)} /></td>
                <td><input value={e.version}
                    onChange={(ev) => set(s.code, "version", ev.target.value)} /></td>
                <td><input type="number" step="0.001" value={e.a}
                    onChange={(ev) => set(s.code, "a", ev.target.value)} /></td>
                <td><input type="number" step="0.01" value={e.b}
                    onChange={(ev) => set(s.code, "b", ev.target.value)} /></td>
                <td><input type="number" step="0.01" value={e.c}
                    onChange={(ev) => set(s.code, "c", ev.target.value)} /></td>
                <td><input type="number" step="0.1" value={e.dbh_min_cm}
                    onChange={(ev) => set(s.code, "dbh_min_cm", ev.target.value)} /></td>
                <td><input type="number" step="0.1" value={e.dbh_max_cm}
                    onChange={(ev) => set(s.code, "dbh_max_cm", ev.target.value)} /></td>
                <td><input type="number" step="0.01" value={e.residual_sigma}
                    onChange={(ev) => set(s.code, "residual_sigma", ev.target.value)} /></td>
                <td><input value={e.citation}
                    onChange={(ev) => set(s.code, "citation", ev.target.value)} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="actions">
        <button disabled={busy} onClick={submit}>Create candidate</button>
        <button disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </section>
  );
}

// ------------------------------------------------------------- detail
function ReviewDetail({ review, comparisons, confirmedVersions, busy,
                        onAction, onReload }) {
  const [baselineId, setBaselineId] = useState(
    confirmedVersions[0] ? confirmedVersions[0].id : "");
  const [openCmp, setOpenCmp] = useState(null);

  const terminal = review.status === "approved"
                   || review.status === "withdrawn";

  async function act(fn) { await onAction(fn); }

  return (
    <section className={`review-detail ${review.status}`}>
      <h3>Review #{review.id} — {review.label} <Badge value={review.status} /></h3>

      <div className="review-controls">
        {review.status === "candidate" && (
          <button disabled={busy} onClick={() => act(async () => {
            await api.validateReview(review.id);
          })}>Validate candidate</button>
        )}
        {!terminal && (
          <span className="compare-bar">
            <label>lock baseline:{" "}
              <select value={baselineId}
                      onChange={(e) => setBaselineId(e.target.value)}>
                {confirmedVersions.map((v) => (
                  <option key={v.id} value={v.id}>
                    #{v.id} {v.label}
                  </option>))}
              </select>
            </label>
            <button disabled={busy || review.status === "candidate"
                                || !baselineId}
                    onClick={() => act(async () => {
                      const c = await api.compareReview(review.id,
                        { baseline_version: Number(baselineId) });
                      setOpenCmp(c.id);
                    })}>
              Run impact comparison
            </button>
          </span>
        )}
        {review.status === "validated" && (
          <button className="primary" disabled={busy} onClick={() => act(
            async () => { await api.approveReview(review.id); })}>
            Approve &amp; mint new edition
          </button>
        )}
        {!terminal && (
          <button disabled={busy} onClick={() => act(async () => {
            await api.withdrawReview(review.id);
          })}>Withdraw</button>
        )}
      </div>

      {review.validation_payload && (
        <details>
          <summary>Validation checks
            ({review.validation_payload.checks.filter((c) => c.passed).length}
            /{review.validation_payload.checks.length} passed)</summary>
          <ul className="check-list">
            {review.validation_payload.checks.map((c, i) => (
              <li key={i} className={c.passed ? "pass" : "fail"}>
                {c.passed ? "✓" : "✗"} {c.name}
                {c.detail && <small> — {c.detail}</small>}
              </li>
            ))}
          </ul>
        </details>
      )}

      <section>
        <h4>Comparisons (locked baselines, coverage matrix, diffs)</h4>
        {comparisons.length === 0 && <p className="hint">None yet.</p>}
        {comparisons.map((cmp) => (
          <ComparisonCard key={cmp.id} cmp={cmp}
                          baselineLabel={(confirmedVersions.find(
                            (v) => v.id === cmp.baseline_version) || {}).label
                            || ""}
                          expanded={openCmp === cmp.id}
                          onToggle={() => setOpenCmp(
                            openCmp === cmp.id ? null : cmp.id)} />
        ))}
      </section>

      <section>
        <h4>History / audit trail</h4>
        <ol className="event-list">
          {review.events.map((e) => (
            <li key={e.id}>
              <code>{new Date(e.created_at).toLocaleString()}</code>{" "}
              <strong>{e.event}</strong>
              {e.actor && <span> by {e.actor}</span>}
              {e.note && <span> — {e.note}</span>}
            </li>
          ))}
        </ol>
      </section>
    </section>
  );
}

// ------------------------------------------------------------- comparison
function ComparisonCard({ cmp, baselineLabel, expanded, onToggle }) {
  const d = cmp.difference_payload;
  return (
    <div className={`comparison ${cmp.status}`}>
      <header onClick={onToggle}>
        <Badge value={cmp.status} />
        <span>baseline edition #{cmp.baseline_version}
          {baselineLabel ? ` (${baselineLabel})` : ""} ·{" "}
          {new Date(cmp.created_at).toLocaleString()}</span>
        {cmp.baseline_reproduced
          ? <span className="ok-chip">baseline reproduced (no data/identity/frame drift)</span>
          : <span className="err-chip">baseline NOT reproduced</span>}
        <span className="toggle">{expanded ? "▾" : "▸"}</span>
      </header>

      {cmp.status === "incomplete" && (
        <div className="error">
          Coverage incomplete — approval forbidden:
          <ul>
            {cmp.incomplete_reasons.map((r, i) => (
              <li key={i}>{r.detail}
                {r.species && <> ({r.species.join(", ")})</>}
                {r.trees && <ul>
                  {r.trees.map((t, j) => (
                    <li key={j}>{t.tree} ({t.species}) dbh {t.dbh_cm} cm,
                      candidate range {t.candidate_range_cm.join("–")} cm</li>
                  ))}
                </ul>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {expanded && (
        <>
          <p className="hint">{d.source_attribution}</p>

          <h5>Coverage matrix (species × dbh class)</h5>
          <table className="matrix-table">
            <thead>
              <tr><th>species</th><th>baseline eq</th><th>candidate eq</th>
                <th>trees / meas.</th><th>observed dbh (cm)</th>
                <th>candidate dbh (cm)</th><th>dbh classes</th><th>cover</th>
              </tr>
            </thead>
            <tbody>
              {cmp.coverage_matrix.rows.map((r) => (
                <tr key={r.species} className={r.covered ? "covered" : "uncovered"}>
                  <td><strong>{r.species}</strong></td>
                  <td>{r.baseline_equation}</td>
                  <td>{r.candidate_equation || "— MISSING —"}</td>
                  <td>{r.n_trees} / {r.n_measurements}</td>
                  <td>{r.observed_dbh_range_cm
                      ? `${r.observed_dbh_range_cm[0]}–${r.observed_dbh_range_cm[1]}`
                      : "—"}</td>
                  <td>{r.candidate_dbh_range_cm
                      ? `${r.candidate_dbh_range_cm[0]}–${r.candidate_dbh_range_cm[1]}`
                      : "—"}</td>
                  <td>
                    {r.dbh_classes.map((k) => (
                      <span key={k.dbh_class_cm}
                        className={`class-chip ${k.candidate_covers_class
                          ? "covered" : "uncovered"}`}
                        title={k.out_of_range_trees.join(", ")}>
                        {k.dbh_class_cm} ({k.n_measurements})
                      </span>
                    ))}
                  </td>
                  <td>{r.covered
                      ? <span className="ok-chip">covered</span>
                      : <span className="err-chip">
                          {r.missing_species ? "missing species"
                            : `${r.out_of_range_count} out of range`}
                        </span>}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h5>Population difference (Mg)</h5>
          <table className="diff-table">
            <thead>
              <tr><th>component</th><th>baseline</th><th>candidate</th>
                <th>Δ</th><th>Δ %</th></tr>
            </thead>
            <tbody>
              {Object.entries(d.population.components).map(([k, v]) => (
                <DiffRow key={k} name={k.replace(/_/g, " ")} x={v.total_mg} />
              ))}
              <DiffRow name="NET CHANGE" x={d.population.net_change.total_mg}
                       strong />
              <DiffRow name="stock t1" x={d.population.stocks.t1.t1_mg} />
              <DiffRow name="stock t2" x={d.population.stocks.t2.t2_mg} />
            </tbody>
          </table>

          <h5>Per-plot difference (kg)</h5>
          <table className="diff-table">
            <thead>
              <tr><th>plot</th><th>stratum</th><th>area ha</th>
                <th>growth base</th><th>growth cand</th><th>Δ growth</th>
                <th>mortality Δ</th><th>ingrowth Δ</th>
                <th>candidate extrapolations</th></tr>
            </thead>
            <tbody>
              {d.plots.map((p) => (
                <tr key={p.plot}>
                  <td>{p.plot}</td><td>{p.stratum}</td>
                  <td>{p.area_ha}</td>
                  <td>{num(p.kg.survivor_growth.baseline)}</td>
                  <td>{num(p.kg.survivor_growth.candidate)}</td>
                  <td>{num(p.kg.survivor_growth.delta)}</td>
                  <td>{num(p.kg.mortality.delta)}</td>
                  <td>{num(p.kg.ingrowth.delta)}</td>
                  <td>{p.candidate_extrapolations.join(", ") || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>

            <h5>Tree-level difference</h5>
            <table className="diff-table tree-diff">
              <thead>
                <tr><th>tree</th><th>sp</th><th>occasion</th><th>component</th>
                  <th>dbh cm</th><th>baseline eq</th><th>candidate eq</th>
                  <th>AGB base kg</th><th>AGB cand kg</th><th>Δ kg</th><th>Δ %</th>
                  <th>coverage</th></tr>
              </thead>
              <tbody>
                {d.trees.map((t, i) => (
                  <tr key={i} className={t.candidate_missing_species
                      ? "uncovered" : t.candidate_out_of_range ? "oor" : ""}>
                    <td>{t.tree}</td><td>{t.species}</td>
                    <td>{t.occasion}</td><td>{t.component}</td>
                    <td>{num(t.dbh_cm, 1)}</td>
                    <td>{t.baseline_equation}</td>
                    <td>{t.candidate_equation || "—"}</td>
                    <td>{num(t.baseline_agb_kg)}</td>
                    <td>{num(t.candidate_agb_kg)}</td>
                    <td>{num(t.delta_agb_kg)}</td>
                    <td>{pct(t.delta_percent)}</td>
                    <td>{t.candidate_missing_species
                        ? <span className="err-chip">missing</span>
                        : t.candidate_out_of_range
                          ? <span className="warn-chip">out of range</span>
                          : <span className="ok-chip">ok</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <h5>Locked inputs</h5>
            <pre className="fingerprints">{JSON.stringify({
              locked: d.locked,
              baseline_version: d.baseline_version_id,
              candidate_run: d.candidate_run,
              fingerprints: cmp.lock_fingerprints,
            }, null, 2)}</pre>
          </>
        )}
      </div>
    );
}

function DiffRow({ name, x, strong }) {
  return (
    <tr className={strong ? "net" : ""}>
      <td>{name}</td>
      <td>{num(x.baseline)}</td>
      <td>{num(x.candidate)}</td>
      <td>{num(x.delta)}</td>
      <td>{pct(x.delta_percent)}</td>
    </tr>
  );
}
