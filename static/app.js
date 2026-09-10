const initialPoint = [39.1547, -77.2405];
const map = L.map("map").setView(initialPoint, 9);
const tileMessage = document.getElementById("map-tile-message");
let tileCycleSucceeded = false;
let tileWarningTimer = null;
const tileLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: "© OpenStreetMap",
  detectRetina: true,
  keepBuffer: 3,
  updateWhenIdle: false
}).addTo(map);

tileLayer.on("loading", () => {
  tileCycleSucceeded = false;
  window.clearTimeout(tileWarningTimer);
});
tileLayer.on("tileload", () => {
  tileCycleSucceeded = true;
  tileMessage.hidden = true;
  window.clearTimeout(tileWarningTimer);
});
tileLayer.on("tileerror", () => {
  window.clearTimeout(tileWarningTimer);
  tileWarningTimer = window.setTimeout(() => {
    if (!tileCycleSucceeded) tileMessage.hidden = false;
  }, 500);
});
tileLayer.on("load", () => {
  tileMessage.hidden = tileCycleSucceeded;
});

const resizeMap = () => window.requestAnimationFrame(() => map.invalidateSize({ pan: false }));
new ResizeObserver(resizeMap).observe(document.querySelector(".map-wrap"));
window.addEventListener("resize", resizeMap);

const pointMarker = L.circleMarker(initialPoint, {
  radius: 7, color: "#17332a", weight: 3, fillColor: "#fffefa", fillOpacity: 1
}).addTo(map);
let footprintLayer = null;
let demoRecord = null;
let activeRecord = null;
let dependenciesReady = false;
let requestGeneration = 0;
let trendGeneration = 0;
let trendController = null;
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
const solarAngleExplanation = "The original helper calculates solar direction differently from its stated convention. LeafView preserves that calculation to match the model's training inputs. Correcting it would require separate evaluation and potentially retraining.";

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
  byId("lai-units").hidden = true;
  byId("result-badge").textContent = "Research estimate";
  byId("lai-explanation").textContent = "LeafView will only display a result whose coordinates and period match the selected query.";
  byId("calculation-source").textContent = "Calculation information will appear with the selected result.";
  byId("provisional-note").textContent = "";
  byId("result-source").textContent = "—";
  byId("result-time").textContent = "—";
  byId("observation-cache").textContent = "—";
  byId("result-location").textContent = selectedQuery.displayLocation;
  byId("requested-point").textContent = formatCoordinates(selectedQuery);
  byId("pixel-center").textContent = "—";
  byId("pixel-index").textContent = "—";
  byId("support-bars").replaceChildren();
  byId("support-summary").textContent = "";
  byId("observation-dates").replaceChildren();
  byId("footprint-corners").replaceChildren();
  byId("processing-diagnostics").open = false;
  byId("observation-details").open = false;
  byId("pixel-details").open = false;
  byId("estimate").disabled = !dependenciesReady;
  refreshSelectedTrend();
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
  requestGeneration += 1;
  const { location, period } = demoRecord.query;
  selectedQuery = {
    latitude: location.latitude,
    longitude: location.longitude,
    periodStart: period.start,
    displayLocation: demoRecord.display_location
  };
  pointMarker.setLatLng([location.latitude, location.longitude]);
  byId("selected-name").textContent = demoRecord.display_location;
  byId("period").value = period.start;
  byId("processing").hidden = true;
  renderResult(demoRecord);
  const bounds = footprintLayer
    ? footprintLayer.getBounds().extend(pointMarker.getLatLng())
    : L.latLngBounds([location.latitude, location.longitude]);
  map.fitBounds(bounds.pad(0.65), { maxZoom: 12 });
  resizeMap();
  refreshSelectedTrend();
}

function renderStatus(data) {
  dependenciesReady = data.ready_for_verified_inference;
  const status = byId("status");
  status.className = `status ${dependenciesReady ? "ready" : "blocked"}`;
  const title = document.createElement("strong");
  title.textContent = dependenciesReady ? "Ready to estimate" : data.headline;
  status.replaceChildren(title);
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
  const readiness = byId("methods-readiness");
  readiness.replaceChildren();
  const readinessItems = [
    data.preprocessing_validation.detail,
    data.historical_numerical_reproduction.detail
  ];
  readinessItems.forEach(text => {
    const item = document.createElement("li");
    item.textContent = text;
    readiness.append(item);
  });
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
    count.textContent = `${passed}/${possible}`;
    row.append(hourLabel, track, count);
    container.append(row);
  });
  const possibleTotal = Number(record.observation_support.possible_total ?? possible * 3);
  const usableDays = Number(record.observation_support.usable_days ?? record.observation_support.usable_dates.length);
  const possibleDays = Number(record.observation_support.possible_days ?? possible);
  byId("support-summary").textContent = `${record.observation_support.passed_total} of ${possibleTotal} observations usable, covering ${usableDays} of ${possibleDays} days.`;
  const dates = byId("observation-dates");
  dates.replaceChildren();
  record.observation_support.usable_dates.forEach(item => {
    const label = document.createElement("span");
    label.className = "observation-date";
    label.textContent = `${formatShortDate(item.date)}: ${item.count} usable observation${item.count === 1 ? "" : "s"}`;
    label.title = `Selected times: ${item.hours_utc.join(", ")} UTC`;
    dates.append(label);
  });
  if (!record.observation_support.usable_dates.length) {
    dates.textContent = "No usable dates.";
  }
}

function renderFootprint(record) {
  byId("result-location").textContent = record.display_location;
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

function renderDiagnostics(record) {
  const timing = Number(record.delivery?.request_seconds || 0);
  byId("result-source").textContent = record.delivery?.cache_hit
    ? "Previously calculated LAI result"
    : "Calculated for this request";
  byId("calculation-source").textContent = record.delivery?.calculation?.summary
    || "Calculation source unavailable.";
  byId("result-time").textContent = `${timing.toFixed(3)} seconds`;
  const observationCache = record.delivery?.observation_cache || {};
  const reused = Number(observationCache.reused || 0);
  const downloaded = Number(observationCache.downloaded || 0);
  const partialRemote = Number(observationCache.partial_remote || 0);
  const partialReused = Number(observationCache.partial_reused || 0);
  const parts = [`${reused} full-file reuses`, `${downloaded} full-file downloads`];
  if (partialRemote || partialReused) {
    parts.push(`${partialRemote} new range reads`, `${partialReused} partial-cache reuses`);
  }
  byId("observation-cache").textContent = parts.join(" · ");
}

function renderMethods(record) {
  const limitations = byId("methods-limitations");
  limitations.replaceChildren();
  if (!record.limitations.length) {
    const item = document.createElement("li");
    item.textContent = "No differences from the supplied preprocessing were identified for this query. The known solar-angle issue below remains.";
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
    item.textContent = text.toLowerCase().includes("solar azimuth")
      ? solarAngleExplanation
      : text;
    concerns.append(item);
  });
}

function renderResult(record) {
  if (!queryMatchesRecord(record)) return false;
  activeRecord = record;
  byId("result-section").classList.remove("awaiting");
  byId("example-meta").textContent = `${record.display_location} · ${formatDate(record.query.period.start)}–${formatDate(record.query.period.end)}`;
  if (record.lai == null) {
    byId("example-lai").textContent = "—";
    byId("lai-units").hidden = true;
    byId("result-badge").textContent = "Insufficient data";
    byId("lai-explanation").textContent = record.data_message || "No research estimate is available for this period.";
  } else {
    const displayed = Number(record.lai.toFixed(2));
    byId("example-lai").textContent = displayed.toFixed(2);
    byId("lai-units").hidden = false;
    byId("result-badge").textContent = "Research estimate";
    byId("lai-explanation").textContent = `About ${Number(record.lai).toFixed(1)} square meters of leaf area per square meter of ground, averaged across this satellite pixel.`;
  }
  byId("provisional-note").textContent = record.status.startsWith("provisional")
    ? "This output has an unresolved preprocessing limitation; see Methods."
    : "";
  renderSupport(record);
  renderFootprint(record);
  renderDiagnostics(record);
  renderMethods(record);
  return true;
}

function svgElement(name, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text != null) node.textContent = text;
  return node;
}

let currentTrend = null;
let renderedTrendWidth = 0;

function formatTrendPeriod(period) {
  const start = new Date(`${period.start}T00:00:00Z`);
  const end = new Date(`${period.end}T00:00:00Z`);
  const startMonth = new Intl.DateTimeFormat("en-US", { month: "short", timeZone: "UTC" }).format(start);
  const endMonth = new Intl.DateTimeFormat("en-US", { month: "short", timeZone: "UTC" }).format(end);
  return startMonth === endMonth
    ? `${startMonth} ${start.getUTCDate()}–${end.getUTCDate()}`
    : `${startMonth} ${start.getUTCDate()}–${endMonth} ${end.getUTCDate()}`;
}

function renderTrend(data) {
  currentTrend = data;
  const periods = data.periods;
  const values = periods.filter(item => item.lai != null).map(item => Number(item.lai));
  const maximum = Math.max(3, values.length ? Math.ceil(Math.max(...values) * 2) / 2 : 3);
  const plot = byId("trend-plot");
  const width = Math.max(360, Math.round(plot.clientWidth || 760));
  renderedTrendWidth = width;
  const height = 270;
  const margin = { top: 26, right: 20, bottom: 82, left: 56 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const yFor = value => margin.top + plotHeight - (Number(value) / maximum) * plotHeight;
  const xPositions = periods.map((_, index) => margin.left + (plotWidth * (index + 0.5)) / periods.length);
  const svg = svgElement("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "Leaf area across three eight-day periods at the selected location"
  });
  svg.append(svgElement("title", {}, "Selected-location leaf area trend"));
  [0, maximum / 3, (maximum * 2) / 3, maximum].forEach(value => {
    const y = yFor(value);
    svg.append(svgElement("line", {
      x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "trend-grid"
    }));
    svg.append(svgElement("text", {
      x: margin.left - 9, y: y + 4, class: "trend-tick"
    }, value.toFixed(1)));
  });
  svg.append(svgElement("line", {
    x1: margin.left, y1: margin.top, x2: margin.left, y2: margin.top + plotHeight, class: "trend-axis"
  }));
  svg.append(svgElement("text", {
    x: 16,
    y: margin.top + plotHeight / 2,
    transform: `rotate(-90 16 ${margin.top + plotHeight / 2})`,
    class: "trend-axis-label"
  }, "LAI (m²/m²)"));
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
      svg.append(svgElement("line", {
        x1: x, y1: margin.top + 25, x2: x, y2: margin.top + plotHeight - 10, class: "trend-gap"
      }));
      svg.append(svgElement("text", {
        x, y: margin.top + plotHeight / 2 + 4, class: "trend-gap-label"
      }, "Gap"));
    } else {
      const y = yFor(Number(item.lai));
      svg.append(svgElement("circle", { cx: x, cy: y, r: 7, class: "trend-point" }));
      svg.append(svgElement("text", { x, y: y - 14, class: "trend-value" }, Number(item.lai).toFixed(2)));
    }
    svg.append(svgElement("text", {
      x,
      y: height - 51,
      class: item.selected ? "trend-date trend-selected-date" : "trend-date"
    }, formatTrendPeriod(item.period)));
    const observationSupport = item.usable_total == null
      ? "Not calculated"
      : `${item.usable_total}/${item.possible_total} obs`;
    const daySupport = item.usable_days == null
      ? ""
      : `${item.usable_days}/${item.possible_days} days`;
    svg.append(svgElement("text", { x, y: height - 32, class: "trend-support" }, observationSupport));
    if (daySupport) {
      svg.append(svgElement("text", { x, y: height - 16, class: "trend-support" }, daySupport));
    }
  });
  plot.replaceChildren(svg);
  byId("trend-meta").textContent = `${data.introduction} Selected period: ${formatDate(data.selected_query.period.start)}–${formatDate(data.selected_query.period.end)}. Requested point: ${formatCoordinates(data.selected_query.location)}. Missing estimates remain gaps.`;
}

function trendResponseMatchesQuery(data, query) {
  const returned = data?.selected_query;
  const selectedPeriod = data?.periods?.find(item => item.selected);
  if (!returned || !selectedPeriod || data.demo !== false) return false;
  return Math.abs(query.latitude - returned.location.latitude) < 0.0000005
    && Math.abs(query.longitude - returned.location.longitude) < 0.0000005
    && query.periodStart === returned.period.start
    && selectedPeriod.period.start === returned.period.start
    && selectedPeriod.query_key === returned.key;
}

function queryStillSelected(query) {
  return Math.abs(query.latitude - selectedQuery.latitude) < 0.0000005
    && Math.abs(query.longitude - selectedQuery.longitude) < 0.0000005
    && query.periodStart === selectedQuery.periodStart;
}

function clearTrendForQuery() {
  currentTrend = null;
  renderedTrendWidth = 0;
  byId("trend-plot").replaceChildren();
  byId("trend-meta").textContent = "Loading the trend for your selected location…";
}

async function refreshSelectedTrend() {
  const generation = ++trendGeneration;
  if (trendController) trendController.abort();
  trendController = null;
  clearTrendForQuery();
  if (!selectedQuery.periodStart) return;
  const query = { ...selectedQuery };
  const controller = new AbortController();
  trendController = controller;
  const parameters = new URLSearchParams({
    latitude: String(query.latitude),
    longitude: String(query.longitude),
    start: query.periodStart,
    display_location: query.displayLocation
  });
  try {
    const response = await fetch(`/api/trends/selected?${parameters}`, {
      cache: "no-store",
      signal: controller.signal
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Trend could not be loaded");
    if (generation !== trendGeneration || !queryStillSelected(query)) return;
    if (!trendResponseMatchesQuery(data, query)) {
      throw new Error("Trend response did not match the selected query");
    }
    renderTrend(data);
  } catch (error) {
    if (error.name === "AbortError" || generation !== trendGeneration) return;
    currentTrend = null;
    byId("trend-plot").replaceChildren();
    byId("trend-meta").textContent = `Trend unavailable for this selection: ${error.message}`;
  } finally {
    if (generation === trendGeneration) trendController = null;
  }
}

new ResizeObserver(entries => {
  const width = Math.round(entries[0]?.contentRect.width || 0);
  if (currentTrend && width && Math.abs(width - renderedTrendWidth) > 1) renderTrend(currentTrend);
}).observe(byId("trend-plot"));

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
    refreshSelectedTrend();
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
  })
]).then(([status, example]) => {
  renderStatus(status);
  demoRecord = example;
  byId("load-example").disabled = false;
  renderPeriods(status, example.query.period.start);
  loadDemoIntoQuery();
}).catch(error => {
  const status = byId("status");
  status.className = "status blocked";
  status.textContent = `The local scientific service is unavailable: ${error.message}`;
});
