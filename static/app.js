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
let demoRecord = null;
let activeRecord = null;
let dependenciesReady = false;
let requestGeneration = 0;
let selectedQuery = {
  latitude: initialPoint[0],
  longitude: initialPoint[1],
  periodStart: null,
  displayLocation: "Montgomery County, Maryland"
};

const byId = (id) => document.getElementById(id);
const delay = (milliseconds) => new Promise(resolve => window.setTimeout(resolve, milliseconds));
const formatCoordinates = ({ latitude, longitude }) =>
  `${latitude.toFixed(4)}, ${longitude.toFixed(4).replace("-", "−")}`;
const formatDate = (value) => new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", year: "numeric", timeZone: "UTC"
}).format(new Date(`${value}T00:00:00Z`));
const formatShortDate = (value) => new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", timeZone: "UTC"
}).format(new Date(`${value}T00:00:00Z`));

function queryMatchesRecord(record) {
  if (!record || !selectedQuery.periodStart) return false;
  const location = record.query.location;
  return Math.abs(selectedQuery.latitude - location.latitude) < 0.0000005 &&
    Math.abs(selectedQuery.longitude - location.longitude) < 0.0000005 &&
    selectedQuery.periodStart === record.query.period.start;
}

function clearFootprint() {
  if (footprintLayer) {
    footprintLayer.remove();
    footprintLayer = null;
  }
}

function resetResultForQuery() {
  requestGeneration += 1;
  activeRecord = null;
  clearFootprint();
  byId("processing").hidden = true;
  const section = byId("result-section");
  section.classList.add("awaiting");
  byId("example-meta").textContent = "No result is attached to this selection yet. Run Estimate LAI.";
  byId("example-lai").textContent = "—";
  byId("result-badge").textContent = "Awaiting query";
  byId("lai-explanation").textContent = "LeafView will only display a result whose coordinates and period match the selected query.";
  byId("provisional-note").textContent = "";
  byId("requested-point").textContent = formatCoordinates(selectedQuery);
  byId("pixel-center").textContent = "—";
  byId("pixel-index").textContent = "—";
  byId("support-bars").replaceChildren();
  byId("support-summary").textContent = "";
  byId("observation-dates").replaceChildren();
  byId("footprint-corners").replaceChildren();
  byId("estimate").disabled = !dependenciesReady;
}

function markCustomSelection(latitude, longitude) {
  selectedQuery = {
    ...selectedQuery,
    latitude: Number(latitude.toFixed(6)),
    longitude: Number(longitude.toFixed(6)),
    displayLocation: "Custom location"
  };
  pointMarker.setLatLng([selectedQuery.latitude, selectedQuery.longitude]);
  byId("selected-name").textContent = selectedQuery.displayLocation;
  byId("selected-coordinates").textContent = formatCoordinates(selectedQuery);
  resetResultForQuery();
}

map.on("click", ({ latlng }) => markCustomSelection(latlng.lat, latlng.lng));

function drawFootprint(record) {
  const corners = record.sampled_pixel?.footprint;
  clearFootprint();
  if (!corners || corners.length !== 4) return;
  const ordered = [corners[0], corners[1], corners[3], corners[2]];
  footprintLayer = L.polygon(ordered.map(corner => [corner.latitude, corner.longitude]), {
    color: "#2d7855", weight: 2, fillColor: "#75a886", fillOpacity: 0.24
  }).addTo(map);
}

function loadDemoIntoQuery() {
  if (!demoRecord) return;
  const { location, period } = demoRecord.query;
  selectedQuery = {
    latitude: location.latitude,
    longitude: location.longitude,
    periodStart: period.start,
    displayLocation: demoRecord.display_location
  };
  pointMarker.setLatLng([location.latitude, location.longitude]);
  byId("selected-name").textContent = demoRecord.display_location;
  byId("selected-coordinates").textContent = formatCoordinates(location);
  byId("period").value = period.start;
  byId("processing").hidden = true;
  renderResult(demoRecord);
  const bounds = footprintLayer
    ? footprintLayer.getBounds().extend(pointMarker.getLatLng())
    : L.latLngBounds([location.latitude, location.longitude]);
  map.fitBounds(bounds.pad(0.65), { maxZoom: 12 });
}

function renderStatus(data) {
  dependenciesReady = data.ready_for_verified_inference;
  const status = byId("status");
  status.className = `status ${data.operational_readiness.ready ? "ready" : "blocked"}`;
  const title = document.createElement("strong");
  title.textContent = data.headline;
  status.replaceChildren(title);
  const checks = document.createElement("p");
  checks.textContent = `Execution: ${data.operational_readiness.status}. Preprocessing: ${data.preprocessing_validation.status}. Historical reproduction: ${data.historical_numerical_reproduction.status} (not required at runtime).`;
  status.append(checks);
  if (data.missing.length) {
    const missing = document.createElement("p");
    missing.textContent = `Still required: ${data.missing.map(item => item.filename).join(" and ")}.`;
    status.append(missing);
  }
  if (data.preprocessing_discrepancies.length) {
    const discrepancy = document.createElement("p");
    discrepancy.textContent = `Unresolved preprocessing: ${data.preprocessing_discrepancies.map(item => item.name).join("; ")}.`;
    status.append(discrepancy);
  }
  byId("estimate").disabled = !dependenciesReady;
}

function renderPeriods(data, selectedStart) {
  const select = byId("period");
  select.replaceChildren();
  data.available_completed_periods.slice().reverse().forEach(period => {
    const option = document.createElement("option");
    option.value = period.start;
    option.textContent = `${formatDate(period.start)} – ${formatDate(period.end)}`;
    select.append(option);
  });
  if (selectedStart && [...select.options].some(option => option.value === selectedStart)) {
    select.value = selectedStart;
  }
  selectedQuery.periodStart = select.value;
  select.addEventListener("change", () => {
    selectedQuery.periodStart = select.value;
    resetResultForQuery();
  });
}

function renderProgress(progress) {
  const panel = byId("processing");
  panel.hidden = false;
  const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  byId("processing-stage").textContent = String(progress.stage || "processing")
    .replaceAll("_", " ")
    .replace(/^./, character => character.toUpperCase());
  byId("processing-percent").textContent = `${Math.round(percent)}%`;
  byId("processing-bar").value = percent;
  byId("processing-detail").textContent = progress.detail || "Processing the selected query…";
}

function renderSupport(record) {
  const container = byId("support-bars");
  container.replaceChildren();
  const possible = record.observation_support.possible_per_hour;
  ["15", "18", "21"].forEach(hour => {
    const passed = Number(record.observation_support.passed_by_hour[hour]);
    const row = document.createElement("div");
    row.className = "support-row";
    const hourLabel = document.createElement("span");
    hourLabel.textContent = `${hour} UTC`;
    const track = document.createElement("div");
    track.className = "bar-track";
    const fill = document.createElement("div");
    fill.className = "bar-fill";
    fill.style.width = `${100 * passed / possible}%`;
    track.append(fill);
    const count = document.createElement("strong");
    count.textContent = `${passed} of ${possible}`;
    row.append(hourLabel, track, count);
    container.append(row);
  });
  byId("support-summary").textContent = `${record.observation_support.passed_total} of ${possible * 3} observations passed filtering.`;
  const dates = byId("observation-dates");
  dates.replaceChildren();
  record.observation_support.usable_dates.forEach(item => {
    const chip = document.createElement("span");
    chip.className = "observation-date";
    chip.textContent = `${formatShortDate(item.date)} · ${item.count}`;
    chip.title = `${item.count} usable observation${item.count === 1 ? "" : "s"}; ${item.hours_utc.join(", ")} UTC`;
    dates.append(chip);
  });
  if (!record.observation_support.usable_dates.length) {
    dates.textContent = "None";
  }
}

function renderFootprint(record) {
  byId("requested-point").textContent = formatCoordinates(record.query.location);
  byId("pixel-center").textContent = record.sampled_pixel.center
    ? formatCoordinates(record.sampled_pixel.center)
    : "Unavailable";
  const row = record.sampled_pixel.row;
  const column = record.sampled_pixel.column;
  byId("pixel-index").textContent = row == null || column == null
    ? "Unavailable"
    : `row ${row}, column ${column}`;
  const list = byId("footprint-corners");
  list.replaceChildren();
  (record.sampled_pixel.footprint || []).forEach(corner => {
    const item = document.createElement("li");
    item.textContent = formatCoordinates(corner);
    list.append(item);
  });
  drawFootprint(record);
}

function renderMethods(record) {
  const limitations = byId("methods-limitations");
  limitations.replaceChildren();
  if (!record.limitations.length) {
    const item = document.createElement("li");
    item.textContent = "No unresolved preprocessing limitation was identified for this query.";
    limitations.append(item);
  } else {
    record.limitations.forEach(text => {
      const item = document.createElement("li");
      item.textContent = text;
      limitations.append(item);
    });
  }
  const concerns = byId("methods-concerns");
  concerns.replaceChildren();
  (record.scientific_concerns || []).forEach(text => {
    const item = document.createElement("li");
    item.textContent = text;
    concerns.append(item);
  });
}

function renderResult(record) {
  if (!queryMatchesRecord(record)) return false;
  activeRecord = record;
  byId("result-section").classList.remove("awaiting");
  const timing = Number(record.delivery?.request_seconds || 0).toFixed(2);
  const delivery = record.delivery?.cache_hit ? `cached · ${timing}s` : `processed · ${timing}s`;
  byId("example-meta").textContent = `${record.display_location} · ${formatDate(record.query.period.start)}–${formatDate(record.query.period.end)} · ${delivery}`;
  if (record.lai == null) {
    byId("example-lai").textContent = "—";
    byId("result-badge").textContent = "Insufficient data";
    byId("lai-explanation").textContent = record.data_message || "No research estimate is available for this period.";
  } else {
    const displayed = Number(record.lai.toFixed(2));
    byId("example-lai").textContent = displayed.toFixed(2);
    byId("result-badge").textContent = "Research estimate";
    byId("lai-explanation").textContent = `An LAI of ${displayed.toFixed(2)} represents an estimated ${displayed.toFixed(2)} square meters of leaf area per square meter of ground within this satellite pixel.`;
  }
  byId("provisional-note").textContent = record.status.startsWith("provisional")
    ? "This output has an unresolved preprocessing limitation; see Methods."
    : "";
  renderSupport(record);
  renderFootprint(record);
  renderMethods(record);
  return true;
}

function svgElement(name, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text != null) node.textContent = text;
  return node;
}

function renderTrend(data) {
  const periods = data.periods;
  const values = periods.filter(item => item.lai != null).map(item => Number(item.lai));
  const minimum = values.length ? Math.min(...values) : 0;
  const maximum = values.length ? Math.max(...values) : 1;
  const span = Math.max(maximum - minimum, 0.2);
  const yFor = value => 105 - ((value - minimum) / span) * 55;
  const xPositions = [90, 300, 510];
  const svg = svgElement("svg", {
    viewBox: "0 0 600 180",
    role: "img",
    "aria-label": "Three consecutive Montgomery County research estimate periods"
  });
  svg.append(svgElement("title", {}, "Montgomery County research estimate trend"));
  for (let index = 0; index < periods.length - 1; index += 1) {
    const left = periods[index];
    const right = periods[index + 1];
    if (left.lai != null && right.lai != null) {
      svg.append(svgElement("line", {
        x1: xPositions[index], y1: yFor(Number(left.lai)),
        x2: xPositions[index + 1], y2: yFor(Number(right.lai)),
        class: "trend-line"
      }));
    }
  }
  periods.forEach((item, index) => {
    const x = xPositions[index];
    if (item.lai == null) {
      svg.append(svgElement("line", { x1: x, y1: 44, x2: x, y2: 112, class: "trend-gap" }));
      svg.append(svgElement("text", { x, y: 80, class: "trend-gap-label" }, "Gap"));
    } else {
      const y = yFor(Number(item.lai));
      svg.append(svgElement("circle", { cx: x, cy: y, r: 7, class: "trend-point" }));
      svg.append(svgElement("text", { x, y: y - 14, class: "trend-value" }, Number(item.lai).toFixed(2)));
    }
    svg.append(svgElement("text", { x, y: 137, class: "trend-date" }, formatShortDate(item.period.start)));
    svg.append(svgElement("text", { x, y: 155, class: "trend-count" }, item.lai == null ? "insufficient data" : `${item.usable_total} usable`));
  });
  byId("trend-plot").replaceChildren(svg);
  byId("trend-meta").textContent = `${data.location.name} · three consecutive completed post-cutoff periods. Missing estimates remain gaps.`;
}

async function pollJob(job, generation) {
  let current = job;
  while (current.state === "running") {
    if (generation !== requestGeneration) return null;
    renderProgress(current.progress);
    await delay(450);
    const response = await fetch(`/api/jobs/${current.job_id}`, { cache: "no-store" });
    current = await response.json();
    if (!response.ok) throw new Error(current.error || "Estimate progress could not be read");
  }
  return current;
}

byId("load-example").addEventListener("click", loadDemoIntoQuery);
byId("estimate").addEventListener("click", async () => {
  const generation = ++requestGeneration;
  const button = byId("estimate");
  button.disabled = true;
  button.textContent = "Estimating…";
  renderProgress({ percent: 0, stage: "starting", detail: "Binding this result to the selected coordinates and period" });
  const requestQuery = {
    latitude: selectedQuery.latitude,
    longitude: selectedQuery.longitude,
    start: selectedQuery.periodStart,
    display_location: selectedQuery.displayLocation
  };
  try {
    const response = await fetch("/api/estimate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestQuery)
    });
    let job = await response.json();
    if (!response.ok) throw new Error(job.error || "Estimate failed");
    job = await pollJob(job, generation);
    if (!job || generation !== requestGeneration) return;
    renderProgress(job.progress);
    if (job.state === "error") throw new Error(job.error || "Estimate failed");
    if (!renderResult(job.result)) {
      throw new Error("The completed result did not match the current selected query and was not displayed");
    }
  } catch (error) {
    if (generation !== requestGeneration) return;
    renderProgress({ percent: 100, stage: "error", detail: error.message });
  } finally {
    if (generation === requestGeneration) {
      button.disabled = !dependenciesReady;
      button.textContent = "Estimate LAI";
    }
  }
});

Promise.all([
  fetch("/api/status", { cache: "no-store" }).then(response => response.json()),
  fetch("/api/examples/montgomery-2026-04-07", { cache: "no-store" }).then(async response => {
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Verified Montgomery record unavailable");
    return body;
  }),
  fetch("/api/trends/montgomery", { cache: "no-store" }).then(response => response.json())
]).then(([status, example, trend]) => {
  renderStatus(status);
  demoRecord = example;
  byId("load-example").disabled = false;
  renderPeriods(status, example.query.period.start);
  loadDemoIntoQuery();
  renderTrend(trend);
}).catch(error => {
  const status = byId("status");
  status.className = "status blocked";
  status.textContent = `The local scientific service is unavailable: ${error.message}`;
});
