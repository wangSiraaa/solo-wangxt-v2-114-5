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

  // equation adoption review (candidate -> validated -> approved/withdrawn)
  reviews: () => get("/equation-reviews/"),
  review: (id) => get(`/equation-reviews/${id}/`),
  reviewComparisons: (id) =>
    get(`/equation-reviews/${id}/comparisons/`),
  createReview: (payload) => post("/equation-reviews/", payload),
  validateReview: (id, payload = {}) =>
    post(`/equation-reviews/${id}/validate/`, payload),
  compareReview: (id, payload) =>
    post(`/equation-reviews/${id}/compare/`, payload),
  withdrawReview: (id, payload = {}) =>
    post(`/equation-reviews/${id}/withdraw/`, payload),
  approveReview: (id, payload = {}) =>
    post(`/equation-reviews/${id}/approve/`, payload),
};
