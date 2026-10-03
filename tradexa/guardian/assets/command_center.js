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
  let lastInstanceLedger = null;

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

  function renderDecisionTraces(page) {
    const body = byId("decision-traces");
    body.replaceChildren();
    const traces = Array.isArray(page.traces) ? page.traces : [];
    byId("decision-coverage").textContent = page.scan_may_be_truncated
      ? "Recent Guardian evidence only; the bounded scan was truncated. No full-history or outcome claim."
      : "Recent Guardian evidence only; earlier or unobserved lifecycle states may be missing. No outcome claim.";
    if (!traces.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 7;
      cell.className = "empty";
      cell.textContent = "No PA/SMC decision evidence received. This does not prove no evaluations occurred.";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    for (const trace of traces) {
      const row = document.createElement("tr");
      textCell(row, displayTime(trace.candle_time));
      textCell(row, `${trace.lab || "Unknown"} · ${trace.strategy_id || "Unknown"} ${trace.strategy_version || ""}`);
      textCell(row, `${trace.symbol || "Unknown"} · ${trace.timeframe || "Unknown"}`);
      textCell(row, `${trace.decision || "UNKNOWN"} · ${trace.reason || "No saved reason"}`);
      textCell(row, Array.isArray(trace.missing_conditions) && trace.missing_conditions.length
        ? trace.missing_conditions.join(", ") : "—");
      textCell(row, trace.near_valid_candidate ? "One missing condition · unproven candidate" : "—");
      textCell(row, trace.event_id);
      body.appendChild(row);
    }
  }

  function renderInstanceDecisionTraces(page) {
    const body = byId("instance-decision-traces");
    body.replaceChildren();
    const traces = Array.isArray(page.traces) ? page.traces : [];
    byId("instance-decision-coverage").textContent = page.scan_may_be_truncated
      ? "Recent persisted decisions only; bounded evidence scan truncated. Broker fills and all evaluations are not proven."
      : "Post-install persisted decisions only; missing source writes and broker fills are not proven.";
    if (!traces.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 7;
      cell.className = "empty";
      cell.textContent = "No instance decision evidence received. This does not prove no decisions occurred.";
      row.appendChild(cell);
      body.appendChild(row);
      return;
    }
    for (const trace of traces) {
      const row = document.createElement("tr");
      textCell(row, displayTime(trace.decision_time));
      textCell(row, `${trace.instance_id || "Unknown"} · ${trace.strategy_id || "Unknown"}`);
      textCell(row, `${trace.symbol || "Unknown"} · ${trace.timeframe || "Unknown"}`);
      textCell(row, trace.strategy_verdict || "UNKNOWN");
      textCell(row, trace.final_state || "UNKNOWN");
      textCell(row, `${trace.gate_stage || "—"} · ${trace.blocker || trace.reason || "No saved blocker"}`);
      textCell(row, trace.event_id);
      body.appendChild(row);
    }
  }

  function renderInstanceLedger(view) {
    const body = byId("instance-ledger");
    body.replaceChildren();
    const current = view.observation_state === "CURRENT";
    const rows = Array.isArray(view.instances) ? view.instances : [];
    const findings = Array.isArray(view.findings) ? view.findings : [];
    byId("instance-ledger-coverage").textContent = current
      ? `Last successful read ${view.observation_age_seconds ?? "unknown"}s ago · ${view.snapshot_atomic ? "atomic source read" : "non-atomic source read; risk unknown"} · Global exposure and currency remain unverified.`
      : "Current paper ledger evidence is unavailable. Any rows below are cached; risk remains unknown.";
    for (const item of rows) {
      const row = document.createElement("tr");
      const owned = findings.filter((finding) => finding.instance_id === item.instance_id);
      const codes = [...new Set(owned.flatMap((finding) => Array.isArray(finding.codes) ? finding.codes : []))];
      textCell(row, item.instance_id);
      textCell(row, item.open_positions);
      textCell(row, item.open_trades);
      textCell(row, current && item.risk_complete && Number.isFinite(item.risk_amount)
        ? item.risk_amount : "Unknown");
      textCell(row, codes.length ? codes.join(", ")
        : current && view.snapshot_atomic ? "Open rows match" : "Unverified");
      textCell(row, view.event_id);
      body.appendChild(row);
    }
    if (!rows.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.className = "empty";
      cell.textContent = current
        ? "No open instance-attributed paper rows were returned by this source read."
        : "No paper ledger observation received.";
      row.appendChild(cell);
      body.appendChild(row);
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
      const [health, eventPage, incidentPage, decisionPage, instanceDecisionPage, instanceLedger] = await Promise.all([
        read("/v1/health"), read("/v1/events?limit=50"), read("/v1/incidents?limit=50"),
        read("/v1/decision-traces?limit=50"),
        read("/v1/instance-decision-traces?limit=50"),
        read("/v1/instance-ledger"),
      ]);
      if (current !== generation || request !== requestSequence || !readKey) return;
      renderComponents(health);
      renderEvents(Array.isArray(eventPage.events) ? eventPage.events : []);
      renderIncidents(Array.isArray(incidentPage.incidents) ? incidentPage.incidents : [],
        health.active_incidents);
      renderDecisionTraces(decisionPage);
      renderInstanceDecisionTraces(instanceDecisionPage);
      lastInstanceLedger = instanceLedger;
      renderInstanceLedger(instanceLedger);
      const observed = health.components && Object.keys(health.components).length > 0;
      setState(health.state === "HEALTHY" && (!health.evidence_complete || !observed)
        ? "UNKNOWN" : health.state);
      byId("last-checked").textContent = new Date().toLocaleTimeString();
      message.textContent = "Connected. Evidence is read-only and refreshed every 15 seconds.";
    } catch (error) {
      if (current !== generation || request !== requestSequence) return;
      setState("UNKNOWN");
      renderInstanceLedger({ ...(lastInstanceLedger || {}), observation_state: "UNKNOWN" });
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
    lastInstanceLedger = null;
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
    byId("decision-coverage").textContent = "Connect to load saved decision evidence.";
    byId("decision-traces").replaceChildren();
    const decisionRow = document.createElement("tr");
    const decisionCell = document.createElement("td");
    decisionCell.colSpan = 7;
    decisionCell.className = "empty";
    decisionCell.textContent = "Connect to load decision traces.";
    decisionRow.appendChild(decisionCell);
    byId("decision-traces").appendChild(decisionRow);
    byId("instance-decision-coverage").textContent = "Connect to load instance gate evidence.";
    byId("instance-decision-traces").replaceChildren();
    const instanceRow = document.createElement("tr");
    const instanceCell = document.createElement("td");
    instanceCell.colSpan = 7;
    instanceCell.className = "empty";
    instanceCell.textContent = "Connect to load instance decision traces.";
    instanceRow.appendChild(instanceCell);
    byId("instance-decision-traces").appendChild(instanceRow);
    byId("instance-ledger-coverage").textContent = "Connect to load paper ledger evidence.";
    byId("instance-ledger").replaceChildren();
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
