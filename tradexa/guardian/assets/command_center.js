(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const stateBadge = byId("overall-state");
  const message = byId("connection-message");
  const keyInput = byId("read-key");
  const connectButton = byId("connect");
  const disconnectButton = byId("disconnect");
  let readKey = null;
  let refreshTimer = null;
  let generation = 0;
  let requestSequence = 0;

  function setState(state) {
    const normalized = ["HEALTHY", "DEGRADED", "BLOCKED", "FAILED", "UNKNOWN"].includes(state)
      ? state : "UNKNOWN";
    stateBadge.textContent = normalized;
    stateBadge.className = `state state-${normalized.toLowerCase()}`;
  }

  function textCell(row, value) {
    const cell = document.createElement("td");
    cell.textContent = value == null || value === "" ? "—" : String(value);
    row.appendChild(cell);
  }

  function renderComponents(health) {
    const container = byId("components");
    container.replaceChildren();
    const components = health && health.components && typeof health.components === "object"
      ? health.components : {};
    const names = Object.keys(components).sort();
    for (const name of names) {
      const item = components[name];
      const card = document.createElement("article");
      card.className = "component";
      const title = document.createElement("span");
      title.className = "component-name";
      title.textContent = name.replaceAll("_", " ");
      const badge = document.createElement("span");
      const state = ["HEALTHY", "DEGRADED", "BLOCKED", "FAILED", "UNKNOWN"].includes(item.state)
        ? item.state : "UNKNOWN";
      badge.className = `state state-${state.toLowerCase()}`;
      badge.textContent = state;
      const reason = document.createElement("p");
      reason.className = "component-reason";
      reason.textContent = item.reason || (item.age_seconds == null
        ? "No fresh observation" : `Last observed ${item.age_seconds}s ago`);
      card.append(title, badge, reason);
      container.appendChild(card);
    }
    if (!names.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No required components configured; state is UNKNOWN.";
      container.appendChild(empty);
    }
    const complete = names.filter((name) => components[name].state !== "UNKNOWN").length;
    byId("coverage").textContent = `${complete} / ${names.length}`;
    byId("coverage-detail").textContent = health.evidence_complete
      ? "All required components have fresh evidence" : "Missing or stale evidence remains";
  }

  function renderEvents(events) {
    const body = byId("events");
    body.replaceChildren();
    byId("event-count").textContent = String(events.length);
    if (!events.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.className = "empty";
      cell.textContent = "No Guardian events received. This is not proof that trading is healthy.";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    for (const event of events) {
      const row = document.createElement("tr");
      const observed = event.received_at ? new Date(event.received_at) : null;
      textCell(row, observed && !Number.isNaN(observed.valueOf())
        ? observed.toLocaleString() : "Unknown time");
      textCell(row, event.source_service);
      textCell(row, event.event_type);
      textCell(row, event.severity);
      textCell(row, event.reason || event.decision);
      textCell(row, event.event_id);
      body.appendChild(row);
    }
  }

  function displayTime(value) {
    const observed = value ? new Date(value) : null;
    return observed && !Number.isNaN(observed.valueOf())
      ? observed.toLocaleString() : "Unknown time";
  }

  function renderIncidents(incidents, activeSummary) {
    const body = byId("incidents");
    body.replaceChildren();
    const activeCount = activeSummary && Number.isSafeInteger(activeSummary.total)
      && activeSummary.total >= 0 ? activeSummary.total : null;
    byId("incident-count").textContent = activeCount == null ? "—" : String(activeCount);
    if (!incidents.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.className = "empty";
      cell.textContent = "No operational incidents have been observed. Missing source evidence may still be UNKNOWN.";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    for (const incident of incidents) {
      const row = document.createElement("tr");
      textCell(row, displayTime(incident.last_seen_at));
      textCell(row, incident.state);
      textCell(row, incident.title);
      textCell(row, `${incident.root_cause || "Cause not proven"} · ${incident.confidence || "UNKNOWN"}`);
      textCell(row, incident.evidence_count);
      const actionCell = document.createElement("td");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "timeline-control";
      button.textContent = "Show evidence";
      const detailRow = document.createElement("tr");
      detailRow.className = "timeline-row";
      detailRow.hidden = true;
      const detailCell = document.createElement("td");
      detailCell.colSpan = 6;
      detailCell.textContent = "Loading timeline…";
      detailRow.appendChild(detailCell);
      button.addEventListener("click", async () => {
        if (!detailRow.hidden) {
          detailRow.hidden = true;
          button.textContent = "Show evidence";
          return;
        }
        detailRow.hidden = false;
        button.textContent = "Hide evidence";
        const current = generation;
        try {
          const result = await read(`/v1/incidents/${encodeURIComponent(incident.incident_id)}/timeline`);
          if (current !== generation || !readKey || detailRow.hidden) return;
          detailCell.replaceChildren();
          const list = document.createElement("ol");
          list.className = "timeline-list";
          for (const update of result.updates || []) {
            const item = document.createElement("li");
            item.textContent = `${displayTime(update.observed_at)} · ${update.transition} · ${update.summary} · ${update.event_id}`;
            list.appendChild(item);
          }
          if (!list.children.length) detailCell.textContent = "No timeline evidence returned.";
          else detailCell.appendChild(list);
        } catch (_error) {
          if (current === generation && !detailRow.hidden) {
            detailCell.textContent = "Timeline unavailable; retry after evidence service recovers.";
          }
        }
      });
      actionCell.appendChild(button);
      row.appendChild(actionCell);
      body.append(row, detailRow);
    }
  }

  async function read(path) {
    const response = await fetch(path, {
      method: "GET", headers: { "X-Guardian-Key": readKey },
      cache: "no-store", credentials: "omit",
    });
    if (!response.ok) throw new Error(response.status === 401 ? "READ_KEY_REJECTED" : `HTTP_${response.status}`);
    return response.json();
  }

  async function refresh() {
    if (!readKey) return;
    const current = generation;
    const request = ++requestSequence;
    try {
      const [health, eventPage, incidentPage] = await Promise.all([
        read("/v1/health"), read("/v1/events?limit=50"), read("/v1/incidents?limit=50"),
      ]);
      if (current !== generation || request !== requestSequence || !readKey) return;
      renderComponents(health);
      renderEvents(Array.isArray(eventPage.events) ? eventPage.events : []);
      renderIncidents(Array.isArray(incidentPage.incidents) ? incidentPage.incidents : [],
        health.active_incidents);
      const observed = health.components && Object.keys(health.components).length > 0;
      setState(health.state === "HEALTHY" && (!health.evidence_complete || !observed)
        ? "UNKNOWN" : health.state);
      byId("last-checked").textContent = new Date().toLocaleTimeString();
      message.textContent = "Connected. Evidence is read-only and refreshed every 15 seconds.";
    } catch (error) {
      if (current !== generation || request !== requestSequence) return;
      setState("UNKNOWN");
      byId("last-checked").textContent = "Unavailable";
      message.textContent = error.message === "READ_KEY_REJECTED"
        ? "Read key rejected. Reconnect with the correct key." : "Guardian evidence unavailable. Prior observations may be stale.";
      if (error.message === "READ_KEY_REJECTED") {
        disconnect();
        message.textContent = "Read key rejected. Reconnect with the correct key.";
      }
    }
  }

  function disconnect() {
    generation += 1;
    readKey = null;
    keyInput.value = "";
    clearInterval(refreshTimer);
    refreshTimer = null;
    connectButton.disabled = false;
    disconnectButton.disabled = true;
    setState("UNKNOWN");
    byId("coverage").textContent = "—";
    byId("coverage-detail").textContent = "Awaiting authenticated read";
    byId("event-count").textContent = "—";
    byId("incident-count").textContent = "—";
    byId("last-checked").textContent = "—";
    const components = byId("components");
    components.replaceChildren();
    const placeholder = document.createElement("p");
    placeholder.className = "empty";
    placeholder.textContent = "Connect to view Guardian's observed components.";
    components.appendChild(placeholder);
    byId("events").replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty";
    cell.textContent = "Connect to load evidence.";
    row.appendChild(cell);
    byId("events").appendChild(row);
    byId("incidents").replaceChildren();
    const incidentRow = document.createElement("tr");
    const incidentCell = document.createElement("td");
    incidentCell.colSpan = 6;
    incidentCell.className = "empty";
    incidentCell.textContent = "Connect to load incidents.";
    incidentRow.appendChild(incidentCell);
    byId("incidents").appendChild(incidentRow);
    message.textContent = "Disconnected. The read key was cleared from this tab.";
  }

  connectButton.addEventListener("click", () => {
    const value = keyInput.value.trim();
    if (!value) {
      message.textContent = "Enter the Guardian read key.";
      return;
    }
    readKey = value;
    keyInput.value = "";
    connectButton.disabled = true;
    disconnectButton.disabled = false;
    generation += 1;
    refresh();
    clearInterval(refreshTimer);
    refreshTimer = setInterval(refresh, 15000);
  });
  disconnectButton.addEventListener("click", disconnect);
})();
