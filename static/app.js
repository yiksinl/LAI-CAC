const initialPoint = [39.1547, -77.2405];
const montgomery = {
  latitude: initialPoint[0],
  longitude: initialPoint[1],
  displayLocation: "Montgomery County, Maryland"
};
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
let activeRecord = null;
let currentHistory = null;
let renderedTrendWidth = 0;
let dependenciesReady = false;
let latestPeriod = null;
let selectedWindowStart = null;
let requestGeneration = 0;
let historyGeneration = 0;
let automaticEstimateTimer = null;
let selectedQuery = { ...montgomery };

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
const formatMonthLabel = (value) => new Intl.DateTimeFormat("en-US", {
  month: "short", timeZone: "UTC"
}).format(new Date(`${value}T00:00:00Z`));
const solarAngleExplanation = "The original helper calculates solar direction differently from its stated convention. LeafView preserves that calculation to match the model's training inputs. Correcting it would require separate evaluation and potentially retraining.";

function sameLocation(left, right) {
  return Math.abs(left.latitude - right.latitude) < 0.0000005
    && Math.abs(left.longitude - right.longitude) < 0.0000005;
}

function queryMatchesRecord(record, expected) {
  return Boolean(record && expected)
    && sameLocation(record.query.location, expected)
    && sameLocation(selectedQuery, expected)
    && record.query.period.start === expected.periodStart
    && selectedWindowStart === expected.periodStart;
}

function clearFootprint() {
  if (footprintLayer) {
    footprintLayer.remove();
    footprintLayer = null;
  }
}

function resetResultPanel(message = "Loading the latest complete window for this location…") {
  activeRecord = null;
  clearFootprint();
  byId("processing").hidden = true;
  byId("result-section").classList.add("awaiting");
  byId("example-meta").textContent = message;
  byId("example-lai").textContent = "—";
  byId("lai-units").hidden = true;
  byId("result-badge").textContent = "Research estimate";
  byId("lai-explanation").textContent = "Leaf area is the one-sided area of leaves above each square meter of ground.";
  byId("calculation-source").textContent = "Calculation information will appear with the result.";
  byId("provisional-note").textContent = "";
  byId("result-source").textContent = "—";
  byId("result-time").textContent = "—";
  byId("observation-cache").textContent = "—";
  byId("result-location").textContent = selectedQuery.displayLocation;
  byId("requested-point").textContent = formatCoordinates(selectedQuery);
  byId("pixel-center").textContent = "—";
  byId("pixel-index").textContent = "—";
  byId("pixel-area-copy").textContent = "The highlighted box will show the area represented by one satellite pixel.";
  byId("support-bars").replaceChildren();
  byId("support-summary").textContent = "";
  byId("observation-dates").replaceChildren();
  byId("footprint-corners").replaceChildren();
  byId("processing-diagnostics").open = false;
  byId("observation-details").open = false;
  byId("pixel-details").open = false;
  byId("methods-limitations").replaceChildren();
  byId("methods-concerns").replaceChildren();
}

function clearHistory(message = "History will start after the latest estimate.") {
  historyGeneration += 1;
  currentHistory = null;
  renderedTrendWidth = 0;
  byId("trend-plot").replaceChildren();
  byId("history-list").replaceChildren();
  byId("trend-meta").textContent = message;
  byId("history-state").className = "history-state";
  byId("history-state").textContent = "Waiting for the latest result.";
}

function scheduleLatestEstimate() {
  window.clearTimeout(automaticEstimateTimer);
  automaticEstimateTimer = window.setTimeout(() => {
    if (dependenciesReady && latestPeriod) runEstimate();
  }, 450);
}

function resetForLocation() {
  requestGeneration += 1;
  selectedWindowStart = latestPeriod?.start || null;
  resetResultPanel();
  clearHistory();
  byId("result-context").textContent = "Latest at selected location";
  byId("estimate").disabled = !dependenciesReady;
  byId("view-latest").disabled = !dependenciesReady;
}

function selectLocation(location, fit = false) {
  selectedQuery = {
    latitude: Number(location.latitude.toFixed(6)),
    longitude: Number(location.longitude.toFixed(6)),
    displayLocation: location.displayLocation
  };
  pointMarker.setLatLng([selectedQuery.latitude, selectedQuery.longitude]);
  byId("selected-name").textContent = selectedQuery.displayLocation;
  if (fit) map.setView([selectedQuery.latitude, selectedQuery.longitude], 10);
  resetForLocation();
  scheduleLatestEstimate();
}

function markCustomSelection(latitude, longitude) {
  selectLocation({
    latitude,
    longitude,
    displayLocation: "Custom location"
  });
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

function footprintAreaSquareKilometers(corners) {
  if (!corners || corners.length !== 4) return null;
  const ordered = [corners[0], corners[1], corners[3], corners[2]];
  const meanLatitude = ordered.reduce((sum, point) => sum + point.latitude, 0) / ordered.length;
  const radius = 6371;
  const points = ordered.map(point => ({
    x: radius * Math.PI * point.longitude / 180 * Math.cos(Math.PI * meanLatitude / 180),
    y: radius * Math.PI * point.latitude / 180
  }));
  let doubledArea = 0;
  points.forEach((point, index) => {
    const next = points[(index + 1) % points.length];
    doubledArea += point.x * next.y - next.x * point.y;
  });
  return Math.abs(doubledArea) / 2;
}

function renderStatus(data) {
  dependenciesReady = data.ready_for_verified_inference;
  latestPeriod = data.latest_window;
  if (!selectedWindowStart) selectedWindowStart = latestPeriod.start;
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
  byId("latest-window-copy").textContent = `Latest complete window: ${formatDate(latestPeriod.start)}–${formatDate(latestPeriod.end)} UTC. The window runs from 00:00 UTC on ${formatShortDate(latestPeriod.start)} to 00:00 UTC on ${formatShortDate(latestPeriod.end_exclusive_utc.slice(0, 10))}, end exclusive. ${data.archive_note}`;
  const historyDate = byId("history-date");
  historyDate.min = data.historical_date_range.minimum_end;
  historyDate.max = data.historical_date_range.maximum_end;
  historyDate.value = data.historical_date_range.maximum_end;
  byId("view-history-date").disabled = !dependenciesReady;
  byId("view-latest").disabled = !dependenciesReady;
  const readiness = byId("methods-readiness");
  readiness.replaceChildren();
  [
    data.preprocessing_validation.detail,
    data.historical_numerical_reproduction.detail,
    data.rolling_window_accuracy.detail
  ].forEach(text => {
    const item = document.createElement("li");
    item.textContent = text;
    readiness.append(item);
  });
  byId("estimate").disabled = !dependenciesReady;
  byId("load-example").disabled = !dependenciesReady;
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
  byId("processing-detail").textContent = progress.detail || "Processing the selected location…";
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
  const support = record.observation_support;
  byId("support-summary").textContent = `${support.passed_total} of ${support.possible_total} observations usable, covering ${support.usable_days} of ${support.possible_days} days.`;
  const dates = byId("observation-dates");
  dates.replaceChildren();
  support.observation_dates.forEach(item => {
    const label = document.createElement("span");
    label.className = "observation-date";
    const usable = item.usable === 1 ? "1 usable observation" : `${item.usable} usable observations`;
    label.textContent = `${formatShortDate(item.date)}: ${usable} (${item.observations} observed)`;
    label.title = `Observed at ${item.hours_utc.join(", ")} UTC; usable at ${item.usable_hours_utc.join(", ") || "none"} UTC`;
    dates.append(label);
  });
  if (!support.observation_dates.length) dates.textContent = "No satellite observations were available.";
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
  const corners = record.sampled_pixel.footprint || [];
  const area = footprintAreaSquareKilometers(corners);
  byId("pixel-area-copy").textContent = area == null
    ? "The estimate represents one satellite pixel, not an individual yard or tree."
    : `The highlighted satellite pixel represents about ${area.toFixed(1)} square kilometers. The estimate is an average for that whole area, not one yard or tree.`;
  const list = byId("footprint-corners");
  list.replaceChildren();
  corners.forEach(corner => {
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
  const memoryReused = Number(observationCache.memory_reused || 0);
  const parts = [`${reused} full-file reuses`, `${downloaded} full-file downloads`];
  parts.push(`${partialRemote} new range reads`, `${partialReused} partial-cache reuses`);
  if (memoryReused) parts.push(`${memoryReused} overlapping-observation reuses`);
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

function renderResult(record, expected) {
  if (!queryMatchesRecord(record, expected)) return false;
  activeRecord = record;
  byId("result-section").classList.remove("awaiting");
  const period = record.query.period;
  byId("example-title").textContent = record.experience_label;
  byId("result-context").textContent = period.start === latestPeriod.start
    ? "Latest at selected location"
    : "Historical window at selected location";
  byId("example-meta").textContent = `${record.display_location} · ${formatDate(period.start)}–${formatDate(period.end)} UTC · observations selected from these eight dates`;
  if (record.lai == null) {
    byId("example-lai").textContent = "—";
    byId("lai-units").hidden = true;
    byId("result-badge").textContent = "Insufficient data";
    byId("lai-explanation").textContent = record.data_message || "Not enough observations passed the existing checks for this window.";
  } else {
    byId("example-lai").textContent = Number(record.lai).toFixed(2);
    byId("lai-units").hidden = false;
    byId("result-badge").textContent = "Research estimate";
    byId("lai-explanation").textContent = `An LAI of ${Number(record.lai).toFixed(1)} means about ${Number(record.lai).toFixed(1)} square meters of one-sided leaf area above each square meter of ground, averaged across this satellite pixel.`;
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

async function pollEstimate(job, generation) {
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

async function runEstimate(periodStart = null) {
  const generation = ++requestGeneration;
  const start = periodStart || latestPeriod?.start;
  if (!start) return;
  selectedWindowStart = start;
  const expected = {
    ...selectedQuery,
    periodStart: start
  };
  resetResultPanel(periodStart
    ? "Loading this historical eight-day window…"
    : "Loading the latest complete eight-day window…");
  if (currentHistory) renderHistory(currentHistory);
  const button = byId("estimate");
  button.disabled = true;
  button.textContent = periodStart ? "Loading older window…" : "Loading latest…";
  renderProgress({ percent: 0, stage: "starting", detail: "Binding this result to the selected location and UTC dates" });
  const payload = {
    latitude: expected.latitude,
    longitude: expected.longitude,
    display_location: expected.displayLocation
  };
  if (periodStart) payload.start = periodStart;
  try {
    const response = await fetch("/api/estimate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    let job = await response.json();
    if (!response.ok) throw new Error(job.error || "Estimate failed");
    job = await pollEstimate(job, generation);
    if (!job || generation !== requestGeneration || !sameLocation(selectedQuery, expected)) return;
    renderProgress(job.progress);
    if (job.state === "error") throw new Error(job.error || "Estimate failed");
    if (!renderResult(job.result, expected)) {
      throw new Error("The completed result did not match the selected location and dates");
    }
    if (!periodStart) startHistory();
  } catch (error) {
    if (generation !== requestGeneration) return;
    renderProgress({ percent: 100, stage: "error", detail: error.message });
  } finally {
    if (generation === requestGeneration) {
      button.disabled = !dependenciesReady;
      button.textContent = "Get latest estimate";
    }
  }
}

function svgElement(name, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text != null) node.textContent = text;
  return node;
}

function formatTrendPeriod(period) {
  return `${formatShortDate(period.start)}–${formatShortDate(period.end)}`;
}

function historyResponseMatchesQuery(data, query) {
  return Boolean(data?.query?.location && data?.query?.latest_period)
    && sameLocation(data.query.location, query)
    && sameLocation(selectedQuery, query)
    && data.query.latest_period.start === latestPeriod.start;
}

function historyItemText(item) {
  if (item.state === "available") return `${Number(item.lai).toFixed(2)} m²/m²`;
  if (item.state === "insufficient_data") return "Insufficient data";
  if (item.state === "retrieval_error") return "Network or retrieval error";
  if (item.state === "error" || item.state === "processing_error") return "Processing error";
  return "Pending";
}

function historyItemSupport(item) {
  if (item.usable_total == null) return item.state === "loading" ? "Pending" : "No support count";
  return `${item.usable_total}/${item.possible_total} observations · ${item.usable_days}/${item.possible_days} days`;
}

function historyObservationDates(item) {
  if (!item.observation_dates?.length) {
    return item.state === "loading"
      ? "Actual observation dates will appear when this snapshot finishes."
      : "No satellite observations were available for this snapshot.";
  }
  return `Actual observations (UTC): ${item.observation_dates.map(dateItem =>
    `${formatShortDate(dateItem.date)} ${dateItem.usable}/${dateItem.observations} usable`
  ).join(" · ")}`;
}

function renderHistoryList(data) {
  const list = byId("history-list");
  list.replaceChildren();
  data.periods.slice().reverse().forEach(item => {
    const row = document.createElement("details");
    row.className = `history-row ${item.state}`;
    const summary = document.createElement("summary");
    summary.className = "snapshot-summary";
    const month = document.createElement("strong");
    month.textContent = item.snapshot.label;
    const dates = document.createElement("strong");
    dates.textContent = `${formatDate(item.period.start)}–${formatDate(item.period.end)}`;
    const outcome = document.createElement("span");
    outcome.textContent = historyItemText(item);
    const support = document.createElement("span");
    support.textContent = historyItemSupport(item);
    summary.append(month, dates, outcome, support);
    const observationDates = document.createElement("p");
    observationDates.className = "snapshot-observation-dates";
    observationDates.textContent = historyObservationDates(item);
    row.append(summary, observationDates);
    list.append(row);
  });
}

function renderHistory(data) {
  currentHistory = data;
  const periods = data.periods;
  const available = periods.filter(item => item.state === "available");
  const insufficient = periods.filter(item => item.state === "insufficient_data").length;
  const retrievalErrors = periods.filter(item => item.state === "retrieval_error").length;
  const processingErrors = periods.filter(item => item.state === "processing_error" || item.state === "error").length;
  const values = available.map(item => Number(item.lai));
  const maximum = Math.max(3, values.length ? Math.ceil(Math.max(...values) * 2) / 2 : 3);
  const plot = byId("trend-plot");
  const width = Math.max(520, Math.round(plot.clientWidth || 900));
  renderedTrendWidth = width;
  const height = 300;
  const margin = { top: 24, right: 18, bottom: 48, left: 54 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const yFor = value => margin.top + plotHeight - Number(value) / maximum * plotHeight;
  const xFor = index => margin.left + (periods.length === 1 ? plotWidth / 2 : plotWidth * index / (periods.length - 1));
  const svg = svgElement("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "Monthly eight-day leaf-area snapshots for the selected location"
  });
  svg.append(svgElement("title", {}, "Monthly leaf-area snapshots"));
  [0, maximum / 3, maximum * 2 / 3, maximum].forEach(value => {
    const y = yFor(value);
    svg.append(svgElement("line", {
      x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "trend-grid"
    }));
    svg.append(svgElement("text", {
      x: margin.left - 8, y: y + 4, class: "trend-tick"
    }, value.toFixed(1)));
  });
  svg.append(svgElement("text", {
    x: 15,
    y: margin.top + plotHeight / 2,
    transform: `rotate(-90 15 ${margin.top + plotHeight / 2})`,
    class: "trend-axis-label"
  }, "LAI (m²/m²)"));
  for (let index = 0; index < periods.length - 1; index += 1) {
    const left = periods[index];
    const right = periods[index + 1];
    if (left.state === "available" && right.state === "available") {
      svg.append(svgElement("line", {
        x1: xFor(index), y1: yFor(left.lai),
        x2: xFor(index + 1), y2: yFor(right.lai),
        class: "trend-line"
      }));
    }
  }
  periods.forEach((item, index) => {
    const x = xFor(index);
    let y = margin.top + plotHeight;
    let className = "trend-loading";
    if (item.state === "available") {
      y = yFor(item.lai);
      className = "trend-point";
    } else if (item.state === "insufficient_data") {
      className = "trend-insufficient";
    } else if (item.state === "retrieval_error" || item.state === "error" || item.state === "processing_error") {
      className = "trend-error";
    }
    const point = svgElement("circle", {
      cx: x,
      cy: y,
      r: item.period.start === selectedWindowStart ? 5 : 2.5,
      class: className
    });
    point.append(svgElement("title", {}, `${formatTrendPeriod(item.period)} · ${historyItemText(item)} · ${historyItemSupport(item)}`));
    svg.append(point);
  });
  periods.forEach((item, index) => {
    const suffix = item.snapshot.is_latest && !item.snapshot.is_month_end ? "*" : "";
    svg.append(svgElement("text", {
      x: xFor(index), y: height - 15, class: "trend-date"
    }, `${formatMonthLabel(item.period.end)}${suffix}`));
  });
  plot.replaceChildren(svg);

  const scope = data.scope;
  const firstRange = `${formatDate(periods[0].period.start)}–${formatDate(periods[0].period.end)}`;
  const latestRange = `${formatDate(periods[periods.length - 1].period.start)}–${formatDate(periods[periods.length - 1].period.end)}`;
  const outside = scope.outside_scope
    ? ` Windows ending ${formatDate(scope.outside_scope.start)} through ${formatDate(scope.outside_scope.end)} are outside scope because every input observation must be after ${formatDate(scope.training_cutoff)}.`
    : "";
  const latestMarker = periods.at(-1).snapshot.is_month_end
    ? ""
    : " The asterisk marks the latest rolling eight-day window.";
  byId("trend-meta").textContent = `${scope.label} for ${data.query.location.name} run from ${firstRange} through ${latestRange}. ${scope.explanation}${latestMarker}${outside}`;
  const state = byId("history-state");
  if (data.state === "running") {
    state.className = "history-state";
    state.textContent = `Monthly snapshots pending: ${data.progress.completed} of ${data.progress.total} loaded, newest first.`;
  } else if (retrievalErrors || processingErrors) {
    state.className = "history-state error";
    const errors = [];
    if (retrievalErrors) errors.push(`${retrievalErrors} network or retrieval error${retrievalErrors === 1 ? "" : "s"}`);
    if (processingErrors) errors.push(`${processingErrors} processing error${processingErrors === 1 ? "" : "s"}`);
    state.textContent = `Monthly snapshots loaded in ${Number(data.elapsed_seconds).toFixed(1)} seconds with ${errors.join(" and ")}. ${available.length} estimates are available; ${insufficient} snapshots had insufficient data.`;
  } else {
    state.className = "history-state complete";
    state.textContent = `Monthly snapshots loaded in ${Number(data.elapsed_seconds).toFixed(1)} seconds. ${available.length} estimates are available; ${insufficient} snapshots had insufficient data.`;
  }
  renderHistoryList(data);
}

async function startHistory() {
  const generation = ++historyGeneration;
  const query = { ...selectedQuery };
  byId("history-state").className = "history-state";
  byId("history-state").textContent = "Monthly snapshots pending: checking the newest point first.";
  try {
    const response = await fetch("/api/history", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        latitude: query.latitude,
        longitude: query.longitude,
        display_location: query.displayLocation
      })
    });
    let history = await response.json();
    if (!response.ok) throw new Error(history.error || "History could not be started");
    while (history.state === "running") {
      if (generation !== historyGeneration || !sameLocation(selectedQuery, query)) return;
      if (!historyResponseMatchesQuery(history, query)) throw new Error("History response did not match the selected location");
      renderHistory(history);
      await delay(700);
      const poll = await fetch(`/api/history/${history.job_id}`, { cache: "no-store" });
      history = await poll.json();
      if (!poll.ok) throw new Error(history.error || "History progress could not be read");
    }
    if (generation !== historyGeneration || !sameLocation(selectedQuery, query)) return;
    if (!historyResponseMatchesQuery(history, query)) throw new Error("History response did not match the selected location");
    renderHistory(history);
  } catch (error) {
    if (generation !== historyGeneration) return;
    currentHistory = null;
    byId("trend-plot").replaceChildren();
    byId("history-state").className = "history-state error";
    byId("history-state").textContent = `History could not load: ${error.message}`;
  }
}

new ResizeObserver(entries => {
  const width = Math.round(entries[0]?.contentRect.width || 0);
  if (currentHistory && width && Math.abs(width - renderedTrendWidth) > 1) renderHistory(currentHistory);
}).observe(byId("trend-plot"));

byId("load-example").addEventListener("click", () => selectLocation(montgomery, true));
byId("estimate").addEventListener("click", () => runEstimate());
byId("view-latest").addEventListener("click", () => runEstimate());
byId("view-history-date").addEventListener("click", () => {
  const endValue = byId("history-date").value;
  if (!endValue) return;
  const start = new Date(`${endValue}T00:00:00Z`);
  start.setUTCDate(start.getUTCDate() - 7);
  runEstimate(start.toISOString().slice(0, 10));
});

fetch("/api/status", { cache: "no-store" })
  .then(async response => {
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Scientific status unavailable");
    renderStatus(data);
    byId("selected-name").textContent = selectedQuery.displayLocation;
    resetForLocation();
    if (dependenciesReady) runEstimate();
  })
  .catch(error => {
    const status = byId("status");
    status.className = "status blocked";
    status.textContent = `The local scientific service is unavailable: ${error.message}`;
  });
