const initialPoint = [39.1547, -77.2405];
const map = L.map("map").setView(initialPoint, 9);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: "© OpenStreetMap"
}).addTo(map);

const pointMarker = L.circleMarker(initialPoint, {
  radius: 7, color: "#17332a", weight: 3, fillColor: "#fffefa", fillOpacity: 1
}).addTo(map);
let footprintLayer = null;
let exampleRecord = null;
let selectedQuery = { latitude: initialPoint[0], longitude: initialPoint[1], periodStart: null };

const byId = (id) => document.getElementById(id);
const formatCoordinates = ({ latitude, longitude }) =>
  `${latitude.toFixed(4)}, ${longitude.toFixed(4).replace("-", "−")}`;
const formatDate = (value) => new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", year: "numeric", timeZone: "UTC"
}).format(new Date(`${value}T00:00:00Z`));

function markCustomSelection(latitude, longitude) {
  selectedQuery = { ...selectedQuery, latitude, longitude };
  pointMarker.setLatLng([latitude, longitude]);
  byId("selected-name").textContent = "Custom location";
  byId("selected-coordinates").textContent = formatCoordinates({ latitude, longitude });
  if (footprintLayer) {
    footprintLayer.remove();
    footprintLayer = null;
  }
  updateSelectionContext();
}

map.on("click", ({ latlng }) => markCustomSelection(latlng.lat, latlng.lng));

function updateSelectionContext() {
  if (!exampleRecord) return;
  const query = exampleRecord.query;
  const sameLocation = Math.abs(selectedQuery.latitude - query.location.latitude) < 1e-6 &&
    Math.abs(selectedQuery.longitude - query.location.longitude) < 1e-6;
  const samePeriod = selectedQuery.periodStart === query.period.start;
  byId("example-meta").classList.toggle("selection-differs", !(sameLocation && samePeriod));
  const suffix = sameLocation && samePeriod
    ? " · loaded in the selected query"
    : " · separate from the current selected query";
  byId("example-meta").textContent = `${exampleRecord.display_location} · ${formatDate(query.period.start)}–${formatDate(query.period.end)}${suffix}`;
}

function drawFootprint(record) {
  const corners = record.sampled_pixel?.footprint;
  if (!corners || corners.length !== 4) return;
  if (footprintLayer) footprintLayer.remove();
  const ordered = [corners[0], corners[1], corners[3], corners[2]];
  footprintLayer = L.polygon(ordered.map(c => [c.latitude, c.longitude]), {
    color: "#2d7855", weight: 2, fillColor: "#75a886", fillOpacity: 0.24
  }).addTo(map);
}

function loadExampleIntoQuery() {
  const { location, period } = exampleRecord.query;
  selectedQuery = { latitude: location.latitude, longitude: location.longitude, periodStart: period.start };
  pointMarker.setLatLng([location.latitude, location.longitude]);
  byId("selected-name").textContent = exampleRecord.display_location;
  byId("selected-coordinates").textContent = formatCoordinates(location);
  byId("period").value = period.start;
  drawFootprint(exampleRecord);
  const bounds = footprintLayer ? footprintLayer.getBounds().extend(pointMarker.getLatLng()) : L.latLngBounds([location.latitude, location.longitude]);
  map.fitBounds(bounds.pad(0.65), { maxZoom: 12 });
  updateSelectionContext();
}

function renderStatus(data) {
  const status = byId("status");
  status.className = `status ${data.ready_for_verified_inference ? "ready" : "blocked"}`;
  const title = document.createElement("strong");
  title.textContent = data.headline;
  status.replaceChildren(title);
  const verified = document.createElement("p");
  verified.textContent = `Verified: ${data.verified.join("; ")}.`;
  status.append(verified);
  if (data.missing.length) {
    const missing = document.createElement("p");
    missing.textContent = `Still required: ${data.missing.map(item => item.filename).join(" and ")}.`;
    status.append(missing);
  }
  if (data.approximations.length) {
    const approximation = document.createElement("p");
    approximation.textContent = `Cached example only: ${data.approximations.join(" ")}`;
    status.append(approximation);
  }
}

function renderPeriods(data) {
  const select = byId("period");
  select.replaceChildren();
  data.available_completed_periods.slice().reverse().forEach(period => {
    const option = document.createElement("option");
    option.value = period.start;
    option.textContent = `${formatDate(period.start)} – ${formatDate(period.end)}`;
    select.append(option);
  });
  selectedQuery.periodStart = select.value;
  select.addEventListener("change", () => {
    selectedQuery.periodStart = select.value;
    updateSelectionContext();
  });
  byId("estimate").disabled = !data.ready_for_verified_inference;
}

function limitationSummary(record) {
  const details = [];
  if (record.provenance?.igbp?.source?.includes("explicit_argument")) details.push("its land-cover class was supplied manually");
  if (record.provenance?.solar_geometry?.includes("approximation")) details.push("solar geometry was approximated");
  return details.length ? `This example is provisional because ${details.join(" and ")}.` : "";
}

function renderSupport(record) {
  const container = byId("support-bars");
  container.replaceChildren();
  const possible = record.observation_support.possible_per_hour;
  let total = 0;
  ["15", "18", "21"].forEach(hour => {
    const passed = Number(record.observation_support.passed_by_hour[hour]);
    total += passed;
    const row = document.createElement("div");
    row.className = "support-row";
    row.innerHTML = `<span>${hour} UTC</span><div class="bar-track"><div class="bar-fill" style="width:${100 * passed / possible}%"></div></div><strong>${passed} of ${possible}</strong>`;
    container.append(row);
  });
  byId("support-summary").textContent = `${total} of ${possible * 3} observations passed filtering.`;
}

function renderExample(record) {
  exampleRecord = record;
  const displayed = Number(record.lai.toFixed(2));
  byId("example-lai").textContent = displayed.toFixed(2);
  byId("result-badge").textContent = record.status.startsWith("provisional") ? "Provisional example" : "Verified estimate";
  byId("lai-explanation").textContent = `An LAI of ${displayed.toFixed(2)} represents an estimated ${displayed.toFixed(2)} square meters of leaf area per square meter of ground within this satellite pixel.`;
  byId("provisional-note").textContent = limitationSummary(record);
  byId("requested-point").textContent = formatCoordinates(record.query.location);
  byId("pixel-center").textContent = formatCoordinates(record.sampled_pixel.center);
  renderSupport(record);
  const list = byId("methods-limitations");
  list.replaceChildren(...record.limitations.map(text => {
    const item = document.createElement("li"); item.textContent = text; return item;
  }));
  byId("load-example").disabled = false;
  updateSelectionContext();
}

byId("load-example").addEventListener("click", loadExampleIntoQuery);

Promise.all([
  fetch("/api/status", { cache: "no-store" }).then(response => response.json()),
  fetch("/api/examples/montgomery-2026-04-07", { cache: "no-store" }).then(response => response.json())
]).then(([status, example]) => {
  renderStatus(status);
  renderPeriods(status);
  renderExample(example);
}).catch(() => {
  const status = byId("status");
  status.className = "status blocked";
  status.textContent = "The local scientific service is unavailable. Restart LeafView and try again.";
});
