const BASE = "/api";

async function get(path) {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

async function post(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path}: ${res.status}`);
  return data;
}

export const api = {
  plots: () => get("/plots/"),
  measurements: (campaign) =>
    get(`/measurements/?campaign=${encodeURIComponent(campaign)}`),
  campaigns: () => get("/campaigns/"),
  equations: () => get("/equations/"),
  conflicts: (status) =>
    get(`/conflicts/${status ? `?status=${status}` : ""}`),
  resolveConflict: (id, payload) =>
    post(`/conflicts/${id}/resolve/`, payload),
  estimates: () => get("/estimates/"),
  estimate: (id) => get(`/estimates/${id}/`),
  createEstimate: (payload) => post("/estimates/", payload),
  confirmEstimate: (id) => post(`/estimates/${id}/confirm/`),

  // equation adoption review workflow
  candidates: (status) =>
    get(`/candidate-equations/${status ? `?status=${status}` : ""}`),
  candidate: (id) => get(`/candidate-equations/${id}/`),
  createCandidate: (payload) => post("/candidate-equations/", payload),
  validateCandidate: (id) =>
    post(`/candidate-equations/${id}/validate/`, {}),
  withdrawCandidate: (id, note) =>
    post(`/candidate-equations/${id}/withdraw/`, { note: note || "" }),
  candidateEvents: (id) => get(`/candidate-equations/${id}/events/`),
  reviews: (qs) => get(`/equation-reviews/${qs ? `?${qs}` : ""}`),
  review: (id) => get(`/equation-reviews/${id}/`),
  createReview: (payload) => post("/equation-reviews/", payload),
  compareReview: (id) => post(`/equation-reviews/${id}/compare/`, {}),
  approveReview: (id, label) =>
    post(`/equation-reviews/${id}/approve/`, label ? { label } : {}),
  reviewEvents: (id) => get(`/equation-reviews/${id}/events/`),
};
