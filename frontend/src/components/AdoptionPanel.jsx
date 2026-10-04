import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";

const STATUSES = ["candidate", "validated", "approved", "withdrawn"];

function Mg(kg) { return (kg / 1000).toFixed(3); }

export default function AdoptionPanel() {
  const [candidates, setCandidates] = useState([]);
  const [reviews, setReviews] = useState([]);
  const [versions, setVersions] = useState([]);
  const [openCandidate, setOpenCandidate] = useState(null);
  const [openReview, setOpenReview] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const [cs, rs, vs] = await Promise.all([
      api.candidates(), api.reviews(), api.estimates()]);
    setCandidates(cs); setReviews(rs); setVersions(vs);
  }
  useEffect(() => { refresh().catch((e) => setErr(e.message)); }, []);

  const confirmedVersions = useMemo(
    () => versions.filter((v) => v.status === "confirmed"), [versions]);

  async function act(promise) {
    setBusy(true); setErr("");
    try { await promise; await refresh(); }
    catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div>
      <h2>Equation adoption review</h2>
      <p className="hint">
        Propose a new allometric equation, validate it, then compare it
        against ONE locked confirmed edition. The comparison reruns on a
        frozen copy of that edition&apos;s <strong>survey data, identity
        decisions and sampling design</strong>, so any difference can only
        come from the equation. Approval requires <strong>complete
        coverage</strong> and produces an <em>independent new confirmed
        edition</em>; the old edition never changes.
      </p>
      {err && <div className="error">{err}</div>}

      <section>
        <div className="section-head">
          <h3>① Candidate equations</h3>
          <button disabled={busy} onClick={() => setShowCreate((s) => !s)}>
            {showCreate ? "cancel" : "+ new candidate"}
          </button>
        </div>
        {showCreate &&
          <CreateCandidate confirmedSpecies={[]}
            onCreated={async (c) => {
              setShowCreate(false);
              await refresh();
              setOpenCandidate(c.id);
            }} busy={busy} setErr={setErr} />}

        <table className="version-table">
          <thead>
            <tr><th>equation</th><th>species</th><th>dbh range (cm)</th>
              <th>status</th><th>coefficients a·d^b·h^c · σ</th>
              <th>reviews</th><th></th></tr>
          </thead>
          <tbody>
            {candidates.map((c) => (
              <tr key={c.id} className={c.status}
                  onClick={() => setOpenCandidate(
                    openCandidate === c.id ? null : c.id)}>
                <td><strong>{c.code} v{c.version}</strong><br />
                  <small>{c.citation}</small></td>
                <td>{c.species_codes.join(", ")}</td>
                <td>{c.dbh_min_cm}–{c.dbh_max_cm}</td>
                <td className={`cand-${c.status}`}>{c.status}</td>
                <td>{c.a} · d^{c.b} · h^{c.c} · σ{c.residual_sigma}</td>
                <td>{c.n_reviews} ({c.n_open_reviews} open)</td>
                <td onClick={(e) => e.stopPropagation()}>
                  {c.status === "candidate" &&
                    <button disabled={busy}
                      onClick={() => act(api.validateCandidate(c.id))}>
                      validate
                    </button>}
                  {(c.status === "candidate" || c.status === "validated") &&
                    <button className="danger" disabled={busy}
                      onClick={() => {
                        const note = prompt("Withdrawal note (audit):", "");
                        if (note !== null)
                          act(api.withdrawCandidate(c.id, note));
                      }}>withdraw</button>}
                </td>
              </tr>
            ))}
            {candidates.length === 0 &&
              <tr><td colSpan="7" className="hint">No candidates yet.</td></tr>}
          </tbody>
        </table>
        {openCandidate &&
          <CandidateDetail id={openCandidate}
            confirmedVersions={confirmedVersions}
            onChanged={refresh} busy={busy} setBusy={setBusy}
            setErr={setErr}
            onOpenReview={(rid) => { setOpenCandidate(null); setOpenReview(rid); }} />}
      </section>

      <section>
        <h3>② Impact comparisons &amp; decision history</h3>
        <table className="version-table">
          <thead>
            <tr><th>review</th><th>candidate</th><th>baseline (locked)</th>
              <th>coverage</th><th>Δnet (Mg)</th><th>status</th>
              <th>new edition</th><th></th></tr>
          </thead>
          <tbody>
            {reviews.map((r) => (
              <tr key={r.id} className={r.status}
                  onClick={() => setOpenReview(
                    openReview === r.id ? null : r.id)}>
                <td>#{r.id} {r.label}</td>
                <td>{r.candidate_label}</td>
                <td>{r.baseline_label}</td>
                <td className={`cover-${r.coverage_status}`}>
                  {r.coverage_status}</td>
                <td>{r.net_delta_mg == null ? "—" : r.net_delta_mg.toFixed(3)}</td>
                <td className={`status-${r.status === "approved"
                  ? "confirmed" : r.status}`}>{r.status}</td>
                <td>{r.new_version_id ? `#${r.new_version_id}` : "—"}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  {r.status === "open" && r.coverage_status === "complete" &&
                    <button disabled={busy} onClick={() =>
                      act(api.approveReview(r.id).then(refresh))}>
                      approve → new edition
                    </button>}
                </td>
              </tr>
            ))}
            {reviews.length === 0 &&
              <tr><td colSpan="8" className="hint">
                Validate a candidate and open a review against a confirmed
                baseline edition.
              </td></tr>}
          </tbody>
        </table>
        {openReview &&
          <ReviewDetail id={openReview} onChanged={refresh} busy={busy}
            setBusy={setBusy} setErr={setErr} />}
      </section>
    </div>
  );
}

// ------------------------------------------------------------- create form
function CreateCandidate({ onCreated, setErr }) {
  const [form, setForm] = useState({
    code: "FIC-AGB", version: "3.0",
    species_codes: [],
    a: "0.135", b: "2.42", c: "0.60",
    dbh_min_cm: "5", dbh_max_cm: "120",
    height_required: true, residual_sigma: "0.17",
    citation: "",
  });
  const [allSpecies, setAllSpecies] = useState([]);
  useEffect(() => { api.equations().then(() => {}); }, []);
  useEffect(() => {
    fetch("/api/species/").then((r) => r.json()).then((sp) => {
      setAllSpecies(sp);
      setForm((f) => ({ ...f, species_codes: sp.map((s) => s.code) }));
    });
  }, []);
  function set(k, v) { setForm((f) => ({ ...f, [k]: v })); }
  async function submit() {
    const payload = {
      ...form,
      a: parseFloat(form.a), b: parseFloat(form.b), c: parseFloat(form.c),
      dbh_min_cm: parseFloat(form.dbh_min_cm),
      dbh_max_cm: parseFloat(form.dbh_max_cm),
      residual_sigma: parseFloat(form.residual_sigma),
    };
    try {
      const c = await api.createCandidate(payload);
      onCreated(c);
    } catch (e) { setErr(e.message); }
  }
  return (
    <div className="create-form">
      <div className="form-row">
        <label>code <input value={form.code}
          onChange={(e) => set("code", e.target.value)} /></label>
        <label>version <input value={form.version}
          onChange={(e) => set("version", e.target.value)} /></label>
        <label>a <input value={form.a}
          onChange={(e) => set("a", e.target.value)} /></label>
        <label>b <input value={form.b}
          onChange={(e) => set("b", e.target.value)} /></label>
        <label>c <input value={form.c}
          onChange={(e) => set("c", e.target.value)} /></label>
        <label>dbh min <input value={form.dbh_min_cm}
          onChange={(e) => set("dbh_min_cm", e.target.value)} /></label>
        <label>dbh max <input value={form.dbh_max_cm}
          onChange={(e) => set("dbh_max_cm", e.target.value)} /></label>
        <label>σ ln(agb) <input value={form.residual_sigma}
          onChange={(e) => set("residual_sigma", e.target.value)} /></label>
      </div>
      <div className="form-row">
        <span>species:{" "}
          {allSpecies.map((s) => (
            <label key={s.code} className="inline-chk">
              <input type="checkbox" checked={form.species_codes.includes(s.code)}
                onChange={(e) => set("species_codes", e.target.checked
                  ? [...form.species_codes, s.code]
                  : form.species_codes.filter((x) => x !== s.code))} />
              {s.code}
            </label>
          ))}
        </span>
      </div>
      <div className="form-row">
        <label>citation <input className="wide" value={form.citation}
          onChange={(e) => set("citation", e.target.value)}
          placeholder="literature source" /></label>
        <button onClick={submit}>create candidate</button>
      </div>
    </div>
  );
}

// ----------------------------------------------------------- candidate card
function CandidateDetail({ id, confirmedVersions, onChanged, busy,
                           setBusy, setErr, onOpenReview }) {
  const [c, setC] = useState(null);
  const [baselineId, setBaselineId] = useState(null);
  useEffect(() => {
    api.candidate(id).then((x) => { setC(x); });
  }, [id]);
  useEffect(() => {
    if (!baselineId && confirmedVersions.length)
      setBaselineId(confirmedVersions[0].id);
  }, [confirmedVersions, baselineId]);
  if (!c) return null;

  async function openReview() {
    setBusy(true); setErr("");
    try {
      const r = await api.createReview({
        candidate_id: c.id, baseline_version_id: parseInt(baselineId, 10) });
      const compared = await api.compareReview(r.id);
      await onChanged();
      onOpenReview(compared.id);
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div className="sub-card">
      <h4>{c.code} v{c.version} <span className={`badge cand-${c.status}`}>
        {c.status}</span></h4>
      {c.validation_result &&
        <details open>
          <summary>validation ({c.validation_result.passed
            ? "passed" : "FAILED"})</summary>
          <table>
            <tbody>
              {c.validation_result.checks.map((k) => (
                <tr key={k.name} className={k.passed ? "" : "mortality"}>
                  <td>{k.passed ? "✓" : "✗"} {k.name}</td>
                  <td><small>{k.detail}</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>}
      {c.status === "validated" &&
        <div className="form-row">
          <label>baseline locked edition:{" "}
            <select value={baselineId || ""}
              onChange={(e) => setBaselineId(e.target.value)}>
              {confirmedVersions.map((v) =>
                <option key={v.id} value={v.id}>#{v.id} {v.label}</option>)}
            </select>
          </label>
          <button disabled={busy || !baselineId} onClick={openReview}>
            lock baseline &amp; run comparison
          </button>
        </div>}
    </div>
  );
}

// -------------------------------------------------------------- review card
function ReviewDetail({ id, onChanged, busy, setBusy, setErr }) {
  const [r, setR] = useState(null);
  const [tab, setTab] = useState("matrix");
  useEffect(() => { api.review(id).then(setR); }, [id]);
  if (!r) return null;
  const cmp = r.comparison;
  const complete = r.coverage_status === "complete";

  async function approve() {
    setBusy(true); setErr("");
    try {
      await api.approveReview(r.id);
      await onChanged();
      setR(await api.review(r.id));
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <div className="sub-card">
      <h4>Review #{r.id} — {r.label}
        <span className={`badge cover-${r.coverage_status}`}>
          {r.coverage_status}</span>
        <span className={`badge status-${r.status === "approved"
          ? "confirmed" : r.status}`}>{r.status}</span>
      </h4>
      <p className="hint">
        🔒 lock <code>{r.lock_checksum.slice(0, 16)}…</code> ·
        compared {r.compared_at ? r.compared_at.slice(0, 19) : "—"} ·
        baseline {r.baseline_label}
      </p>

      {r.status === "open" && !complete &&
        <div className="error">Coverage incomplete:{" "}
          {cmp ? cmp.coverage.incomplete_reasons.join("; ")
            : "no comparison yet"} — approval is forbidden.</div>}
      {r.status === "withdrawn" &&
        <div className="error">Candidate withdrawn. The comparison below is
          retained for audit and can never be confirmed.</div>}
      {r.status === "approved" &&
        <div className="ok">Approved — new confirmed edition
          #{r.new_version_id} generated; the baseline edition is unchanged.
        </div>}

      {!cmp && <p className="hint">Comparison has not run.</p>}
      {cmp && (
        <>
          <DiffSources cmp={cmp} />
          <PopulationTable cmp={cmp} />
          <nav className="mini-tabs">
            {["matrix", "per_plot", "per_tree", "history"].map((t) =>
              <button key={t} className={tab === t ? "tab active" : "tab"}
                onClick={() => setTab(t)}>
                {t === "matrix" ? "coverage matrix"
                  : t === "per_plot" ? "per plot"
                  : t === "per_tree" ? `per tree (${cmp.per_tree.length})`
                  : "history"}
              </button>)}
          </nav>
          {tab === "matrix" && <CoverageMatrix cmp={cmp} />}
          {tab === "per_plot" && <PerPlot cmp={cmp} />}
          {tab === "per_tree" && <PerTree cmp={cmp} />}
          {tab === "history" && <History reviewId={r.id} />}

          {r.status === "open" &&
            <div className="form-row">
              <button className={complete ? "" : "danger"}
                disabled={busy || !complete}
                title={complete ? "" : "incomplete coverage blocks approval"}
                onClick={approve}>
                {complete
                  ? "approve & generate new confirmed edition"
                  : "approval blocked (incomplete coverage)"}
              </button>
            </div>}
        </>
      )}
    </div>
  );
}

function DiffSources({ cmp }) {
  const d = cmp.diff_sources;
  return (
    <div className="diff-sources">
      <h5>Difference attribution (locked frame)</h5>
      <ul>
        <li>survey data drift: {d.data_drift}</li>
        <li>identity decision drift: {d.identity_drift}</li>
        <li>sampling-frame drift: {d.frame_drift}</li>
        <li className={d.equation_only ? "ok-line" : "bad-line"}>
          baseline reproduction on locked frame:{" "}
          {d.baseline_reproduction.passed ? "PASS" : "FAIL"}
          <ul>
            {d.baseline_reproduction.checks.map((k) =>
              <li key={k.quantity} className={k.passed ? "" : "mortality"}>
                {k.quantity}: |Δ|={k.abs_diff_kg.toExponential(2)} kg
                {" "}{k.passed ? "✓" : "✗"}
              </li>)}
          </ul>
        </li>
      </ul>
    </div>
  );
}

function PopulationTable({ cmp }) {
  const pop = cmp.population;
  return (
    <table className="component-table">
      <thead><tr><th>component</th><th>baseline Mg</th>
        <th>candidate Mg</th><th>Δ Mg</th><th>Δ %</th></tr></thead>
      <tbody>
        {Object.entries(pop.components).map(([k, v]) => (
          <tr key={k}><td>{k.replace(/_/g, " ")}</td>
            <td>{v.baseline_mg.toFixed(3)}</td>
            <td>{v.candidate_mg.toFixed(3)}</td>
            <td className={v.delta_mg >= 0 ? "pos" : "neg"}>
              {v.delta_mg.toFixed(3)}</td>
            <td>{v.delta_percent == null ? "—" : `${v.delta_percent}%`}</td>
          </tr>
        ))}
        <tr className="net"><td>NET CHANGE</td>
          <td>{pop.net_change.baseline_mg.toFixed(3)}</td>
          <td>{pop.net_change.candidate_mg.toFixed(3)}</td>
          <td>{pop.net_change.delta_mg.toFixed(3)}</td><td></td></tr>
        <tr><td>stock t1</td>
          <td>{pop.stocks.t1.baseline_mg.toFixed(3)}</td>
          <td>{pop.stocks.t1.candidate_mg.toFixed(3)}</td>
          <td>{pop.stocks.t1.delta_mg.toFixed(3)}</td><td></td></tr>
        <tr><td>stock t2</td>
          <td>{pop.stocks.t2.baseline_mg.toFixed(3)}</td>
          <td>{pop.stocks.t2.candidate_mg.toFixed(3)}</td>
          <td>{pop.stocks.t2.delta_mg.toFixed(3)}</td><td></td></tr>
      </tbody>
    </table>
  );
}

function CoverageMatrix({ cmp }) {
  const cov = cmp.coverage;
  return (
    <div>
      <p>
        Missing species:{" "}
        <strong className={cov.missing_species.length ? "neg" : "pos"}>
          {cov.missing_species.length ? cov.missing_species.join(", ")
            : "none"}</strong>
        {" "}· out-of-range stems:{" "}
        <strong className={cov.n_out_of_range ? "neg" : "pos"}>
          {cov.n_out_of_range}</strong>
        {" "}· missing-input regressions:{" "}
        <strong className={cov.n_missing_input_regressions ? "neg" : "pos"}>
          {cov.n_missing_input_regressions}</strong>
      </p>
      {Object.entries(cov.matrix).map(([sp, entry]) => (
        <div key={sp} className="matrix-species">
          <h5>{sp} {entry.species_covered
            ? "" : <span className="badge cand-withdrawn">species not covered</span>}
          </h5>
          <table>
            <thead><tr><th>dbh bin (cm)</th><th>n</th><th>covered</th>
              <th>extrapolated</th><th>missing species</th>
              <th>missing input</th></tr></thead>
            <tbody>
              {entry.bins.map((b) => (
                <tr key={b.bin} className={
                  b.missing_species || b.extrapolated || b.missing_input
                    ? "notmeasured" : ""}>
                  <td>{b.bin}</td><td>{b.n}</td><td>{b.covered}</td>
                  <td className={b.extrapolated ? "neg" : ""}>
                    {b.extrapolated}</td>
                  <td className={b.missing_species ? "neg" : ""}>
                    {b.missing_species}</td>
                  <td className={b.missing_input ? "neg" : ""}>
                    {b.missing_input}</td>
                </tr>
              ))}
              {entry.bins.length === 0 &&
                <tr><td colSpan="6" className="hint">no quantified stems</td></tr>}
            </tbody>
          </table>
        </div>
      ))}
      {cov.out_of_range.length > 0 &&
        <details>
          <summary>{cov.out_of_range.length} stems beyond the candidate
            dbh range (extrapolation)</summary>
          <ul>
            {cov.out_of_range.map((o) =>
              <li key={`${o.tag}-${o.role}`}>{o.tag} ({o.species},{" "}
                {o.role}): dbh {o.dbh_cm} cm outside{" "}
                {o.candidate_range_cm[0]}–{o.candidate_range_cm[1]}</li>)}
          </ul>
        </details>}
    </div>
  );
}

function PerPlot({ cmp }) {
  return (
    <table>
      <thead><tr><th>plot</th><th>component</th><th>baseline kg/ha</th>
        <th>candidate kg/ha</th><th>Δ kg/ha</th><th>flags</th></tr></thead>
      <tbody>
        {cmp.per_plot.flatMap((p) =>
          Object.entries(p.components).map(([k, v]) => (
            <tr key={`${p.plot}-${k}`}>
              <td>{p.plot} ({p.area_ha} ha)</td>
              <td>{k.replace(/_/g, " ")}</td>
              <td>{v.baseline_kg_ha.toFixed(1)}</td>
              <td>{v.candidate_kg_ha.toFixed(1)}</td>
              <td className={v.delta_kg_ha >= 0 ? "pos" : "neg"}>
                {v.delta_kg_ha.toFixed(1)}</td>
              <td>
                {p.candidate_extrapolations > 0 &&
                  <span className="chip warn-chip">
                    extrap {p.candidate_extrapolations}</span>}
                {p.missing_species_stems > 0 &&
                  <span className="chip warn-chip">
                    missing sp {p.missing_species_stems}</span>}
              </td>
            </tr>
          )))}
      </tbody>
    </table>
  );
}

function PerTree({ cmp }) {
  const [filter, setFilter] = useState("all");
  const rows = cmp.per_tree.filter((r) =>
    filter === "all"
      ? true
      : filter === "changed" ? r.delta_kg !== null && r.delta_kg !== 0
      : filter === "uncovered"
        ? (!r.candidate_covered_species || r.extrapolates_candidate
           || r.candidate_missing_input)
        : true);
  return (
    <div>
      <nav className="mini-tabs">
        {[["all", `all (${cmp.per_tree.length})`],
          ["changed", "equation-affected"],
          ["uncovered", "coverage gaps"]].map(([k, label]) =>
          <button key={k} className={filter === k ? "tab active" : "tab"}
            onClick={() => setFilter(k)}>{label}</button>)}
      </nav>
      <table>
        <thead><tr><th>tree</th><th>role</th><th>species</th><th>dbh</th>
          <th>baseline kg</th><th>candidate kg</th><th>Δ kg</th>
          <th>coverage</th></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.tag}-${r.role}-${i}`} className={
              !r.candidate_covered_species || r.extrapolates_candidate
                ? "notmeasured" : ""}>
              <td>{r.tag}</td><td>{r.role.replace(/_/g, " ")}</td>
              <td>{r.species}</td><td>{r.dbh_cm ?? "—"}</td>
              <td>{r.agb_baseline_kg ?? "—"}</td>
              <td>{r.agb_candidate_kg ?? "—"}</td>
              <td className={r.delta_kg == null ? ""
                : r.delta_kg >= 0 ? "pos" : "neg"}>
                {r.delta_kg ?? "—"}</td>
              <td>
                {!r.candidate_covered_species &&
                  <span className="chip warn-chip">missing species</span>}
                {r.extrapolates_candidate &&
                  <span className="chip warn-chip">out of range</span>}
                {r.candidate_missing_input &&
                  <span className="chip warn-chip">missing input</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function History({ reviewId }) {
  const [events, setEvents] = useState([]);
  useEffect(() => { api.reviewEvents(reviewId).then(setEvents); }, [reviewId]);
  return (
    <table>
      <thead><tr><th>when</th><th>event</th><th>actor</th>
        <th>note</th><th>detail</th></tr></thead>
      <tbody>
        {events.map((e) => (
          <tr key={e.id}>
            <td>{e.created_at.slice(0, 19)}</td><td>{e.event}</td>
            <td>{e.actor}</td><td>{e.note}</td>
            <td><small><code>{e.payload
              ? JSON.stringify(e.payload).slice(0, 220) : ""}</code></small></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
