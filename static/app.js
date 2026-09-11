const initialPoint = [39.1547, -77.2405];
const montgomery = {
  latitude: initialPoint[0],
  longitude: initialPoint[1],
  displayLocation: "Montgomery County, Maryland",
  county: "Montgomery County",
  placeNameStatus: "ready",
  placeNameSource: null
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
let activeHistoryJobId = null;
let automaticEstimateTimer = null;
let estimateRetryAction = null;
let placeNameGeneration = 0;
let placeNameTimer = null;
let placeNameController = null;
let searchGeneration = 0;
let searchController = null;
let selectedQuery = { ...montgomery };

const byId = (id) => document.getElementById(id);
const delay = (milliseconds) => new Promise(resolve => window.setTimeout(resolve, milliseconds));
const formatCoordinates = ({ latitude, longitude }) =>
  `${latitude.toFixed(4)}, ${longitude.toFixed(4).replace("-", "−")}`;
function locationDetails(location) {
  const details = [];
  if (location.county) details.push(`County: ${location.county}`);
  details.push(`Selected coordinates: ${formatCoordinates(location)}`);
  if (location.placeNameStatus === "loading") details.push("Finding place name…");
  if (location.placeNameStatus === "error") details.push("Place name unavailable");
  return details.join(" · ");
}
const formatDate = (value) => new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", year: "numeric", timeZone: "UTC"
}).format(new Date(`${value}T00:00:00Z`));
const formatShortDate = (value) => new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", timeZone: "UTC"
}).format(new Date(`${value}T00:00:00Z`));
const formatMonthLabel = (value) => new Intl.DateTimeFormat("en-US", {
  month: "short", timeZone: "UTC"
}).format(new Date(`${value}T00:00:00Z`));
const formatObservationPeriod = (startValue, endValue) => {
  const start = new Date(`${startValue}T00:00:00Z`);
  const end = new Date(`${endValue}T00:00:00Z`);
  const month = new Intl.DateTimeFormat("en-US", { month: "long", timeZone: "UTC" });
  const startMonth = month.format(start);
  const endMonth = month.format(end);
  const startYear = start.getUTCFullYear();
  const endYear = end.getUTCFullYear();
  if (startMonth === endMonth && startYear === endYear) {
    return `${startMonth} ${start.getUTCDate()}–${end.getUTCDate()}, ${endYear}`;
  }
  if (startYear === endYear) {
    return `${startMonth} ${start.getUTCDate()}–${endMonth} ${end.getUTCDate()}, ${endYear}`;
  }
  return `${startMonth} ${start.getUTCDate()}, ${startYear}–${endMonth} ${end.getUTCDate()}, ${endYear}`;
};
const formatCalculationTimestamp = (value) => {
  if (!value) return null;
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return null;
  return new Intl.DateTimeFormat("en-US", {
    month: "short", day: "numeric", year: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit",
    timeZone: "UTC", timeZoneName: "short"
  }).format(timestamp);
};
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

function renderSelectedLocationCopy() {
  const details = locationDetails(selectedQuery);
  byId("selected-name").textContent = selectedQuery.displayLocation;
  byId("selected-location-details").textContent = details;
  byId("place-name-attribution").hidden = selectedQuery.placeNameSource !== "OpenStreetMap";
  byId("result-location").textContent = selectedQuery.displayLocation;
  byId("result-location-details").textContent = details;
  if (activeRecord && sameLocation(activeRecord.query.location, selectedQuery)) {
    const period = activeRecord.query.period;
    byId("example-meta").textContent = `Observation period: ${formatDate(period.start)}–${formatDate(period.end)} UTC`;
  }
  if (currentHistory && sameLocation(currentHistory.query.location, selectedQuery)) {
    renderHistory(currentHistory);
  }
}

function resetResultPanel(message = "Loading the latest complete window for this location…") {
  activeRecord = null;
  clearFootprint();
  byId("processing").hidden = true;
  estimateRetryAction = null;
  byId("retry-estimate").hidden = true;
  byId("result-section").classList.add("awaiting");
  byId("example-meta").textContent = message;
  byId("example-lai").textContent = "—";
  byId("lai-units").hidden = true;
  byId("result-badge").textContent = "Research estimate";
  byId("lai-explanation").textContent = "Leaf area is the one-sided area of leaves above each square meter of ground.";
  byId("calculation-source").textContent = "";
  byId("calculation-source").hidden = true;
  byId("calculated-at").textContent = "";
  byId("calculated-at").hidden = true;
  byId("provisional-note").textContent = "";
  byId("result-source").textContent = "—";
  byId("result-time").textContent = "—";
  byId("observation-cache").textContent = "—";
  byId("result-location").textContent = selectedQuery.displayLocation;
  byId("result-location-details").textContent = locationDetails(selectedQuery);
  byId("requested-point").textContent = formatCoordinates(selectedQuery);
  byId("pixel-center").textContent = "—";
  byId("pixel-index").textContent = "—";
  byId("pixel-area-copy").textContent = "The highlighted box will show the area represented by one satellite pixel.";
  byId("support-bars").replaceChildren();
  byId("support-summary").textContent = "";
  byId("observation-total").textContent = "";
  byId("observation-period-dates").replaceChildren();
  byId("observation-dates").replaceChildren();
  byId("footprint-corners").replaceChildren();
  byId("processing-diagnostics").open = false;
  byId("pixel-details").open = false;
}

function showEstimateState(label, state = "ready", retryAction = null) {
  byId("processing").hidden = true;
  const status = byId("status");
  status.hidden = false;
  status.className = `status ${state}`;
  const title = document.createElement("strong");
  title.textContent = label;
  status.replaceChildren(title);
  estimateRetryAction = retryAction;
  byId("retry-estimate").hidden = retryAction == null;
}

function clearHistory(message = "History will start after the latest estimate.") {
  cancelActiveHistory();
  historyGeneration += 1;
  currentHistory = null;
  renderedTrendWidth = 0;
  renderHistoryFrame(message);
  byId("history-list").replaceChildren();
  byId("trend-meta").textContent = message;
  byId("history-state").className = "history-state";
  byId("history-state").textContent = "Waiting for the latest result.";
  byId("retry-history").hidden = true;
}

function cancelActiveHistory() {
  const jobId = activeHistoryJobId;
  activeHistoryJobId = null;
  requestHistoryCancellation(jobId);
}

function requestHistoryCancellation(jobId) {
  if (!jobId) return;
  fetch(`/api/history/${jobId}/cancel`, {
    method: "POST",
    keepalive: true
  }).catch(() => {});
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
  if (dependenciesReady) {
    renderProgress({
      stage: "Preparing estimate",
      percent: 0,
      detail: "Starting the latest estimate for the selected location…"
    });
  }
  byId("view-latest").disabled = !dependenciesReady;
}

function cancelPlaceNameLookup() {
  window.clearTimeout(placeNameTimer);
  placeNameTimer = null;
  if (placeNameController) placeNameController.abort();
  placeNameController = null;
}

async function resolvePlaceName(expected, generation) {
  const controller = new AbortController();
  placeNameController = controller;
  try {
    const response = await fetch("/api/place-name", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        latitude: expected.latitude,
        longitude: expected.longitude
      }),
      signal: controller.signal
    });
    const place = await response.json();
    if (generation !== placeNameGeneration || !sameLocation(selectedQuery, expected)) return;
    if (!response.ok) throw new Error(place.error || "Place name lookup unavailable");
    if (!place.requested_location || !sameLocation(place.requested_location, expected)) {
      throw new Error("Place name response did not match the selected coordinates");
    }
    const displayLocation = String(place.display_name || "").trim();
    if (!displayLocation) throw new Error("Place name lookup returned no supported area");
    selectedQuery = {
      ...selectedQuery,
      displayLocation,
      county: String(place.county || "").trim() || null,
      placeNameStatus: "ready",
      placeNameSource: "OpenStreetMap"
    };
    renderSelectedLocationCopy();
  } catch (error) {
    if (error.name === "AbortError"
        || generation !== placeNameGeneration
        || !sameLocation(selectedQuery, expected)) return;
    selectedQuery = {
      ...selectedQuery,
      displayLocation: formatCoordinates(selectedQuery),
      county: null,
      placeNameStatus: "error",
      placeNameSource: null
    };
    renderSelectedLocationCopy();
  } finally {
    if (generation === placeNameGeneration) placeNameController = null;
  }
}

function schedulePlaceNameLookup(expected, generation) {
  placeNameTimer = window.setTimeout(() => {
    placeNameTimer = null;
    resolvePlaceName(expected, generation);
  }, 175);
}

function selectLocation(location, fit = false, resolveName = false) {
  cancelPlaceNameLookup();
  const latitude = Number(location.latitude.toFixed(6));
  const longitude = Number(location.longitude.toFixed(6));
  const coordinates = { latitude, longitude };
  const generation = ++placeNameGeneration;
  selectedQuery = {
    ...coordinates,
    displayLocation: resolveName
      ? formatCoordinates(coordinates)
      : location.displayLocation,
    county: resolveName ? null : (location.county || null),
    placeNameStatus: resolveName ? "loading" : "ready",
    placeNameSource: resolveName ? null : (location.placeNameSource || null)
  };
  pointMarker.setLatLng([selectedQuery.latitude, selectedQuery.longitude]);
  renderSelectedLocationCopy();
  if (fit) map.setView([selectedQuery.latitude, selectedQuery.longitude], 10);
  resetForLocation();
  scheduleLatestEstimate();
  if (resolveName) schedulePlaceNameLookup({ ...selectedQuery }, generation);
}

function markCustomSelection(latitude, longitude) {
  selectLocation({
    latitude,
    longitude
  }, false, true);
}

map.on("click", ({ latlng }) => markCustomSelection(latlng.lat, latlng.lng));

document.querySelectorAll("[data-open-details]").forEach(link => {
  link.addEventListener("click", () => {
    const target = byId(link.dataset.openDetails);
    if (target) target.open = true;
  });
});

function normalizedSearchText(value) {
  return String(value || "").trim().replace(/\s+/g, " ");
}

function coordinateSearch(value) {
  const match = normalizedSearchText(value).match(
    /^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*,\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))$/
  );
  if (!match) return null;
  const latitude = Number(match[1]);
  const longitude = Number(match[2]);
  return {
    matched: true,
    valid: Number.isFinite(latitude) && Number.isFinite(longitude)
      && latitude >= -90 && latitude <= 90
      && longitude >= -180 && longitude <= 180,
    latitude,
    longitude
  };
}

function clearSearchSuggestions() {
  const suggestions = byId("location-search-suggestions");
  suggestions.replaceChildren();
  suggestions.hidden = true;
  byId("location-search-input").setAttribute("aria-expanded", "false");
  byId("location-search-attribution").hidden = true;
}

function setSearchStatus(message, error = false) {
  const status = byId("location-search-status");
  status.textContent = message;
  status.className = `location-search-status${error ? " error" : ""}`;
  status.hidden = !message;
}

function cancelPlaceSearch() {
  searchGeneration += 1;
  if (searchController) searchController.abort();
  searchController = null;
  byId("location-search-submit").disabled = false;
}

function chooseSearchSuggestion(suggestion) {
  cancelPlaceSearch();
  clearSearchSuggestions();
  byId("location-search-input").value = suggestion.display_name;
  setSearchStatus(`Selected ${suggestion.display_name}. Loading its estimate and history.`);
  selectLocation({
    latitude: Number(suggestion.latitude),
    longitude: Number(suggestion.longitude),
    displayLocation: suggestion.display_name,
    county: suggestion.county || null,
    placeNameSource: "OpenStreetMap"
  }, true, false);
}

function renderSearchSuggestions(results) {
  const list = byId("location-search-suggestions");
  list.replaceChildren();
  results.forEach(suggestion => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "location-suggestion";
    const name = document.createElement("strong");
    name.textContent = suggestion.display_name;
    const context = document.createElement("span");
    context.textContent = suggestion.county ? `County: ${suggestion.county}` : "Select this location";
    button.append(name, context);
    button.addEventListener("click", () => chooseSearchSuggestion(suggestion));
    item.append(button);
    list.append(item);
  });
  list.hidden = false;
  byId("location-search-input").setAttribute("aria-expanded", "true");
  byId("location-search-attribution").hidden = false;
}

async function submitLocationSearch() {
  const input = byId("location-search-input");
  const query = normalizedSearchText(input.value);
  clearSearchSuggestions();
  const coordinates = coordinateSearch(query);
  if (coordinates?.matched) {
    cancelPlaceSearch();
    if (!coordinates.valid) {
      setSearchStatus("Enter latitude from −90 to 90 and longitude from −180 to 180.", true);
      return;
    }
    input.value = `${coordinates.latitude.toFixed(6)}, ${coordinates.longitude.toFixed(6)}`;
    setSearchStatus("Coordinates selected. Loading the estimate and history.");
    selectLocation(coordinates, true, true);
    return;
  }
  if (query.length < 2) {
    setSearchStatus("Enter at least two characters or latitude and longitude.", true);
    return;
  }

  cancelPlaceSearch();
  const generation = searchGeneration;
  const controller = new AbortController();
  searchController = controller;
  byId("location-search-submit").disabled = true;
  setSearchStatus("Searching U.S. locations…");
  try {
    const response = await fetch(`/api/place-search?q=${encodeURIComponent(query)}`, {
      cache: "no-store",
      signal: controller.signal
    });
    const data = await response.json();
    if (generation !== searchGeneration
        || normalizedSearchText(input.value) !== query) return;
    if (!response.ok) throw new Error(data.error || "Place search is unavailable");
    if (data.query !== query) throw new Error("Search response did not match the current search");
    if (!data.results?.length) {
      setSearchStatus("No matching place found. Try a nearby town or enter coordinates.", true);
      return;
    }
    renderSearchSuggestions(data.results);
    setSearchStatus(`${data.results.length} matching U.S. location${data.results.length === 1 ? "" : "s"}. Choose one below.`);
  } catch (error) {
    if (error.name === "AbortError" || generation !== searchGeneration) return;
    setSearchStatus("Place search is unavailable. Try coordinates or choose a point on the map.", true);
  } finally {
    if (generation === searchGeneration) {
      searchController = null;
      byId("location-search-submit").disabled = false;
    }
  }
}

byId("location-search").addEventListener("submit", event => {
  event.preventDefault();
  submitLocationSearch();
});
byId("location-search-input").addEventListener("input", () => {
  cancelPlaceSearch();
  clearSearchSuggestions();
  setSearchStatus("Press Search to find a U.S. location. Typing does not start an estimate.");
});
byId("location-search-input").addEventListener("keydown", event => {
  if (event.key === "Escape") {
    cancelPlaceSearch();
    clearSearchSuggestions();
    setSearchStatus("Search suggestions closed.");
  }
});

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
  showEstimateState(dependenciesReady ? "Ready to estimate" : data.headline,
    dependenciesReady ? "ready" : "blocked");
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
  byId("latest-window-copy").textContent = `Latest observation period: ${formatObservationPeriod(latestPeriod.start, latestPeriod.end)} UTC.`;
  byId("latest-window-details").textContent = `UTC boundaries: 00:00 on ${formatDate(latestPeriod.start)} through 00:00 on ${formatDate(latestPeriod.end_exclusive_utc.slice(0, 10))} (end exclusive). ${data.archive_note}`;
  const historyDate = byId("history-date");
  historyDate.min = data.historical_date_range.minimum_end;
  historyDate.max = data.historical_date_range.maximum_end;
  historyDate.value = data.historical_date_range.maximum_end;
  byId("view-history-date").disabled = !dependenciesReady;
  byId("view-latest").disabled = !dependenciesReady;
}

function renderProgress(progress) {
  const panel = byId("processing");
  byId("status").hidden = true;
  estimateRetryAction = null;
  byId("retry-estimate").hidden = true;
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
  const periodDates = [];
  const currentDate = new Date(`${record.query.period.start}T00:00:00Z`);
  const finalDate = new Date(`${record.query.period.end}T00:00:00Z`);
  while (currentDate <= finalDate) {
    periodDates.push(currentDate.toISOString().slice(0, 10));
    currentDate.setUTCDate(currentDate.getUTCDate() + 1);
  }

  const periodDateList = byId("observation-period-dates");
  periodDateList.replaceChildren();
  periodDates.forEach(date => {
    const label = document.createElement("span");
    label.className = "observation-period-date";
    label.textContent = formatDate(date);
    periodDateList.append(label);
  });

  // Count distinct strict-pass dates from the result metadata; observation totals
  // cannot be divided by the three daily time slots to obtain this value.
  const distinctUsableDates = new Set(
    (support.observation_dates || [])
      .filter(item => Number(item.usable) > 0)
      .map(item => item.date)
  );
  (support.usable_dates || []).forEach(item => {
    distinctUsableDates.add(typeof item === "string" ? item : item.date);
  });
  distinctUsableDates.delete(undefined);
  distinctUsableDates.delete(null);
  byId("support-summary").textContent = `Usable observations from ${distinctUsableDates.size} of ${support.possible_days} days`;
  byId("observation-total").textContent = `${support.passed_total} of ${support.possible_total} expected observations were usable.`;

  const dates = byId("observation-dates");
  dates.replaceChildren();
  const metadataByDate = new Map((support.observation_dates || []).map(item => [item.date, item]));
  periodDates.forEach(date => {
    const item = metadataByDate.get(date) || {
      date,
      observations: 0,
      usable: 0,
      hours_utc: [],
      usable_hours_utc: []
    };
    const label = document.createElement("div");
    label.className = "observation-date";
    const counts = document.createElement("strong");
    counts.textContent = `${formatDate(item.date)}: ${item.usable} of ${item.observations} available observations usable`;
    const times = document.createElement("span");
    const selectedTimes = item.hours_utc?.length ? `${item.hours_utc.join(", ")} UTC` : "none available";
    const usableTimes = item.usable_hours_utc?.length ? `${item.usable_hours_utc.join(", ")} UTC` : "none";
    times.textContent = `Selected observation times: ${selectedTimes}. Usable times: ${usableTimes}.`;
    label.append(counts, times);
    dates.append(label);
  });
}

function renderFootprint(record) {
  byId("result-location").textContent = selectedQuery.displayLocation;
  byId("result-location-details").textContent = locationDetails(selectedQuery);
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
    ? "Saved estimate"
    : "New calculation";
  byId("calculation-source").textContent = record.delivery?.calculation?.summary
    || "Calculation source unavailable.";
  byId("calculation-source").hidden = false;
  const calculated = formatCalculationTimestamp(record.calculated_at_utc);
  byId("calculated-at").textContent = calculated
    ? `Calculated: ${calculated}`
    : "Calculation time unavailable.";
  byId("calculated-at").hidden = false;
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

function renderResult(record, expected) {
  if (!queryMatchesRecord(record, expected)) return false;
  activeRecord = record;
  byId("result-section").classList.remove("awaiting");
  const period = record.query.period;
  byId("result-context").textContent = period.start === latestPeriod.start
    ? "Latest eight-day estimate"
    : "Historical eight-day estimate";
  byId("example-title").textContent = "Estimated leaf area";
  byId("example-meta").textContent = `Observation period: ${formatDate(period.start)}–${formatDate(period.end)} UTC`;
  if (record.lai == null) {
    byId("example-lai").textContent = "—";
    byId("lai-units").hidden = true;
    byId("result-badge").textContent = "Insufficient data";
    byId("lai-explanation").textContent = record.data_message || "Not enough observations passed the existing checks for this window.";
  } else {
    byId("example-lai").textContent = Number(record.lai).toFixed(2);
    byId("lai-units").hidden = false;
    byId("result-badge").textContent = "Research estimate";
    byId("lai-explanation").textContent = `About ${Number(record.lai).toFixed(1)} square meters of leaves for each square meter of ground.`;
  }
  byId("provisional-note").textContent = record.status.startsWith("provisional")
    ? "This output has an unresolved preprocessing limitation."
    : "";
  renderSupport(record);
  renderFootprint(record);
  renderDiagnostics(record);
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
  renderProgress({
    stage: periodStart ? "Loading older window" : "Loading latest estimate",
    percent: 0,
    detail: periodStart
      ? "Starting the selected eight-day estimate…"
      : "Starting the latest estimate for the selected location…"
  });
  if (currentHistory) renderHistory(currentHistory);
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
    if (job.state === "error") throw new Error(job.error || "Estimate failed");
    if (!renderResult(job.result, expected)) {
      throw new Error("The completed result did not match the selected location and dates");
    }
    showEstimateState("Estimate ready");
    if (!periodStart) startHistory();
  } catch (error) {
    if (generation !== requestGeneration) return;
    showEstimateState(
      `Estimate unavailable: ${error.message}`,
      "blocked",
      () => runEstimate(periodStart)
    );
  }
}

function svgElement(name, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text != null) node.textContent = text;
  return node;
}

function renderHistoryFrame(message = "Monthly snapshots will appear here.") {
  const plot = byId("trend-plot");
  const width = Math.max(520, Math.round(plot.clientWidth || 900));
  renderedTrendWidth = width;
  const height = 230;
  const margin = { top: 22, right: 18, bottom: 42, left: 54 };
  const plotHeight = height - margin.top - margin.bottom;
  const svg = svgElement("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "Monthly eight-day leaf-area snapshot chart"
  });
  svg.append(svgElement("title", {}, "Monthly leaf-area snapshots"));
  [0, 1, 2, 3].forEach(value => {
    const y = margin.top + plotHeight - value / 3 * plotHeight;
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
  svg.append(svgElement("text", {
    x: width / 2,
    y: margin.top + plotHeight / 2,
    class: "trend-placeholder"
  }, message));
  plot.replaceChildren(svg);
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
  const renderStarted = performance.now();
  currentHistory = data;
  const periods = data.periods;
  const available = periods.filter(item => item.state === "available");
  const insufficient = periods.filter(item => item.state === "insufficient_data").length;
  const pending = periods.filter(item => item.state === "loading").length;
  const retrievalErrors = periods.filter(item => item.state === "retrieval_error").length;
  const processingErrors = periods.filter(item => item.state === "processing_error" || item.state === "error").length;
  const historyFailed = data.state === "error";
  const values = available.map(item => Number(item.lai));
  const maximum = Math.max(3, values.length ? Math.ceil(Math.max(...values) * 2) / 2 : 3);
  const plot = byId("trend-plot");
  const width = Math.max(520, Math.round(plot.clientWidth || 900));
  renderedTrendWidth = width;
  const height = 230;
  const margin = { top: 22, right: 18, bottom: 42, left: 54 };
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
    point.append(svgElement("title", {}, `${formatTrendPeriod(item.period)} · ${historyItemText(item)}`));
    svg.append(point);
  });
  periods.forEach((item, index) => {
    const suffix = item.snapshot.is_latest && !item.snapshot.is_month_end ? "*" : "";
    svg.append(svgElement("text", {
      x: xFor(index), y: height - 15, class: "trend-date"
    }, `${formatMonthLabel(item.period.end)}${suffix}`));
  });
  plot.replaceChildren(svg);
  plot.dataset.renderMilliseconds = (performance.now() - renderStarted).toFixed(1);

  const scope = data.scope;
  const firstRange = `${formatDate(periods[0].period.start)}–${formatDate(periods[0].period.end)}`;
  const latestRange = `${formatDate(periods[periods.length - 1].period.start)}–${formatDate(periods[periods.length - 1].period.end)}`;
  const outside = scope.outside_scope
    ? ` Windows ending ${formatDate(scope.outside_scope.start)} through ${formatDate(scope.outside_scope.end)} are outside scope because every input observation must be after ${formatDate(scope.training_cutoff)}.`
    : "";
  const latestMarker = periods.at(-1).snapshot.is_month_end
    ? ""
    : " The asterisk marks the latest rolling eight-day window.";
  const locationName = sameLocation(data.query.location, selectedQuery)
    ? selectedQuery.displayLocation
    : data.query.location.name;
  byId("trend-meta").textContent = `${scope.label} for ${locationName} run from ${firstRange} through ${latestRange}. ${scope.explanation}${latestMarker}${outside}`;
  const state = byId("history-state");
  if (data.state === "running") {
    state.className = "history-state";
    state.textContent = `Loading history: ${data.progress.monthly_completed} of ${data.progress.monthly_total} monthly snapshots ready.`;
  } else if (historyFailed || retrievalErrors || processingErrors) {
    state.className = "history-state error";
    const errors = [];
    if (historyFailed) errors.push("history processing stopped");
    if (retrievalErrors) errors.push(`${retrievalErrors} network or retrieval error${retrievalErrors === 1 ? "" : "s"}`);
    if (processingErrors) errors.push(`${processingErrors} processing error${processingErrors === 1 ? "" : "s"}`);
    state.textContent = `Monthly snapshots loaded in ${Number(data.elapsed_seconds).toFixed(1)} seconds with ${errors.join(" and ")}. ${available.length} estimates are available; ${insufficient} snapshots had insufficient data; ${pending} are still pending.`;
  } else {
    state.className = "history-state complete";
    state.textContent = `Monthly snapshots loaded in ${Number(data.elapsed_seconds).toFixed(1)} seconds. ${available.length} estimates are available; ${insufficient} snapshots had insufficient data.`;
  }
  byId("retry-history").hidden = data.state === "running"
    || !(historyFailed || retrievalErrors || processingErrors);
  renderHistoryList(data);
}

async function startHistory() {
  const generation = ++historyGeneration;
  const query = { ...selectedQuery };
  let historyJobId = null;
  activeHistoryJobId = null;
  byId("retry-history").hidden = true;
  byId("history-state").className = "history-state";
  byId("history-state").textContent = "Loading history: checking monthly snapshots.";
  try {
    const response = await fetch("/api/history", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        latitude: query.latitude,
        longitude: query.longitude,
        display_location: query.displayLocation,
        latest_query_key: activeRecord?.query?.key
      })
    });
    let history = await response.json();
    if (!response.ok) throw new Error(history.error || "History could not be started");
    historyJobId = history.job_id;
    if (generation !== historyGeneration || !sameLocation(selectedQuery, query)) {
      requestHistoryCancellation(history.job_id);
      return;
    }
    activeHistoryJobId = history.job_id;
    while (history.state === "running") {
      if (generation !== historyGeneration || !sameLocation(selectedQuery, query)) {
        requestHistoryCancellation(history.job_id);
        if (activeHistoryJobId === history.job_id) activeHistoryJobId = null;
        return;
      }
      if (!historyResponseMatchesQuery(history, query)) throw new Error("History response did not match the selected location");
      renderHistory(history);
      await delay(250);
      const poll = await fetch(`/api/history/${history.job_id}`, { cache: "no-store" });
      history = await poll.json();
      if (!poll.ok) throw new Error(history.error || "History progress could not be read");
    }
    if (generation !== historyGeneration || !sameLocation(selectedQuery, query)) {
      requestHistoryCancellation(history.job_id);
      if (activeHistoryJobId === history.job_id) activeHistoryJobId = null;
      return;
    }
    if (!historyResponseMatchesQuery(history, query)) throw new Error("History response did not match the selected location");
    renderHistory(history);
    if (activeHistoryJobId === history.job_id) activeHistoryJobId = null;
  } catch (error) {
    requestHistoryCancellation(historyJobId);
    if (generation !== historyGeneration) return;
    if (activeHistoryJobId === historyJobId) activeHistoryJobId = null;
    currentHistory = null;
    renderHistoryFrame("Monthly snapshots could not be loaded.");
    byId("history-state").className = "history-state error";
    byId("history-state").textContent = `History could not load: ${error.message}`;
    byId("retry-history").hidden = false;
  }
}

new ResizeObserver(entries => {
  const width = Math.round(entries[0]?.contentRect.width || 0);
  if (currentHistory && width && Math.abs(width - renderedTrendWidth) > 1) renderHistory(currentHistory);
}).observe(byId("trend-plot"));

byId("compare-landscapes").addEventListener("click", event => {
  event.preventDefault();
  const guide = byId("landscape-guide");
  guide.scrollIntoView({
    behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    block: "start"
  });
  byId("lai-reference-title").focus({ preventScroll: true });
});
byId("retry-estimate").addEventListener("click", () => {
  const retry = estimateRetryAction;
  if (retry) retry();
});
byId("retry-history").addEventListener("click", () => startHistory());
byId("view-latest").addEventListener("click", () => runEstimate());
byId("view-history-date").addEventListener("click", () => {
  const endValue = byId("history-date").value;
  if (!endValue) return;
  const start = new Date(`${endValue}T00:00:00Z`);
  start.setUTCDate(start.getUTCDate() - 7);
  runEstimate(start.toISOString().slice(0, 10));
});

async function loadStatus() {
  showEstimateState("Checking scientific dependencies…", "loading");
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Scientific status unavailable");
    renderStatus(data);
    renderSelectedLocationCopy();
    resetForLocation();
    if (dependenciesReady) runEstimate();
  } catch (error) {
    showEstimateState(
      `The local scientific service is unavailable: ${error.message}`,
      "blocked",
      loadStatus
    );
  }
}

renderHistoryFrame("History will start after the latest estimate.");
loadStatus();
