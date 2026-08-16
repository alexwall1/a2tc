"use strict";

function setText(id, text) {
  var el = document.getElementById(id);
  if (el) {
    el.textContent = text;
  }
}

function wireUploadForm(formId, inputId) {
  var form = document.getElementById(formId);
  if (!form) {
    return;
  }
  var input = document.getElementById(inputId);
  var maxBytes = Number(form.getAttribute("data-max-bytes") || "0");
  form.addEventListener("submit", function (event) {
    var file = input.files[0];
    if (!file) {
      window.alert("Choose a PDF file.");
      event.preventDefault();
      return;
    }
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      window.alert("The file must be a PDF.");
      event.preventDefault();
      return;
    }
    if (maxBytes > 0 && file.size > maxBytes) {
      window.alert("The file is too large.");
      event.preventDefault();
    }
  });
}

function wireCancelForm(formId) {
  var form = document.getElementById(formId);
  if (!form) {
    return;
  }
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    fetch(form.getAttribute("action"), { method: "POST" })
      .then(function () {
        window.location.reload();
      })
      .catch(function () {
        window.location.reload();
      });
  });
}

var pollHandle = null;

function startPolling(jobId, pollSeconds) {
  refreshStatus(jobId);
  pollHandle = window.setInterval(function () {
    refreshStatus(jobId);
  }, pollSeconds * 1000);
}

function refreshStatus(jobId) {
  fetch("/api/jobs/" + encodeURIComponent(jobId))
    .then(function (response) {
      if (!response.ok) {
        return null;
      }
      return response.json();
    })
    .then(function (state) {
      if (!state) {
        return;
      }
      renderState(state);
      refreshLog(jobId);
      if (state.status === "done") {
        refreshReport(jobId);
      }
      if (isTerminal(state.status) && pollHandle !== null) {
        window.clearInterval(pollHandle);
        pollHandle = null;
      }
    })
    .catch(function () {
      /* keep polling; the server may be restarting */
    });
}

function renderState(state) {
  setText("status", state.status);
  setText(
    "created-at",
    state.created_at ? new Date(state.created_at * 1000).toLocaleString() : ""
  );
  setText(
    "started-at",
    state.started_at ? new Date(state.started_at * 1000).toLocaleString() : ""
  );
  setText(
    "finished-at",
    state.finished_at ? new Date(state.finished_at * 1000).toLocaleString() : ""
  );
  if (state.queue_position !== null && state.queue_position !== undefined) {
    setText("queue-position", "Position in queue: " + state.queue_position);
  } else {
    setText("queue-position", "");
  }
  var errorEl = document.getElementById("error");
  if (errorEl) {
    errorEl.hidden = !state.error;
    setText("error", state.error || "");
  }
  renderArtifacts(state.artifacts || []);
}

function renderArtifacts(artifacts) {
  var list = document.getElementById("artifacts");
  if (!list) {
    return;
  }
  list.textContent = "";
  artifacts.forEach(function (artifact) {
    var item = document.createElement("li");
    var link = document.createElement("a");
    link.href = artifact.download_url;
    link.textContent = artifact.title || artifact.name;
    item.appendChild(link);
    if (artifact.title && artifact.title !== artifact.name) {
      var note = document.createElement("span");
      note.className = "artifact-name";
      note.textContent = artifact.name;
      item.appendChild(note);
    }
    list.appendChild(item);
  });
}

function refreshLog(jobId) {
  fetch("/api/jobs/" + encodeURIComponent(jobId) + "/log")
    .then(function (response) {
      if (!response.ok) {
        return "";
      }
      return response.text();
    })
    .then(function (text) {
      setText("log-content", text);
    })
    .catch(function () {});
}

function refreshReport(jobId) {
  var section = document.getElementById("report-section");
  if (!section) {
    return;
  }
  fetch("/api/jobs/" + encodeURIComponent(jobId) + "/report")
    .then(function (response) {
      if (!response.ok) {
        return null;
      }
      return response.text();
    })
    .then(function (text) {
      if (text === null) {
        return;
      }
      var content = document.getElementById("report-content");
      if (content) {
        content.innerHTML = renderMarkdown(text);
      }
      section.hidden = false;
    })
    .catch(function () {});
}

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderInline(text) {
  var parts = text.split(/`([^`]*)`/g);
  for (var i = 1; i < parts.length; i += 2) {
    parts[i] = "<code>" + parts[i] + "</code>";
  }
  for (var i = 0; i < parts.length; i += 2) {
    parts[i] = parts[i]
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  }
  return parts.join("");
}

function tableCells(line) {
  var body = line.trim();
  if (body.charAt(0) === "|") {
    body = body.slice(1);
  }
  if (body.charAt(body.length - 1) === "|") {
    body = body.slice(0, -1);
  }
  var cells = body.split("|");
  for (var i = 0; i < cells.length; i += 1) {
    cells[i] = cells[i].trim();
  }
  return cells;
}

function isTableSeparator(line) {
  if (line.indexOf("|") === -1) {
    return false;
  }
  return tableCells(line).every(function (cell) {
    return cell === "" || /^:?-+:?$/.test(cell);
  });
}

function startsBlock(lines, i) {
  var line = lines[i];
  if (/^(#{1,6})\s+/.test(line)) {
    return true;
  }
  if (line.trim().charAt(0) === "|") {
    return i + 1 < lines.length && isTableSeparator(lines[i + 1]);
  }
  return /^\s*-\s+/.test(line) || /^\s*>\s?/.test(line);
}

function renderMarkdown(markdown) {
  var lines = markdown.split("\n");
  var html = [];
  var i = 0;
  while (i < lines.length) {
    var line = lines[i];
    var trimmed = line.trim();
    if (trimmed === "") {
      i += 1;
      continue;
    }

    var heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      var level = heading[1].length;
      html.push(
        "<h" + level + ">" +
        renderInline(escapeHtml(heading[2].trim())) +
        "</h" + level + ">"
      );
      i += 1;
      continue;
    }

    if (
      trimmed.charAt(0) === "|" &&
      i + 1 < lines.length &&
      isTableSeparator(lines[i + 1])
    ) {
      html.push("<table><thead><tr>");
      tableCells(line).forEach(function (cell) {
        html.push("<th>" + renderInline(escapeHtml(cell)) + "</th>");
      });
      html.push("</tr></thead><tbody>");
      i += 2;
      while (i < lines.length && lines[i].trim().charAt(0) === "|") {
        html.push("<tr>");
        tableCells(lines[i]).forEach(function (cell) {
          if (cell === "") {
            html.push("<td></td>");
          } else {
            html.push("<td>" + renderInline(escapeHtml(cell)) + "</td>");
          }
        });
        html.push("</tr>");
        i += 1;
      }
      html.push("</tbody></table>");
      continue;
    }

    if (/^\s*-\s+/.test(line)) {
      html.push("<ul>");
      while (i < lines.length && /^\s*-\s+/.test(lines[i])) {
        var item = lines[i].replace(/^\s*-\s+/, "");
        html.push("<li>" + renderInline(escapeHtml(item)));
        i += 1;
        while (i < lines.length && /^\s{2,}>\s+/.test(lines[i])) {
          var quote = lines[i].replace(/^\s{2,}>\s+/, "");
          html.push(
            "<blockquote>" + renderInline(escapeHtml(quote)) + "</blockquote>"
          );
          i += 1;
        }
        html.push("</li>");
      }
      html.push("</ul>");
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      var paras = [];
      var para = [];
      while (i < lines.length) {
        var qline = lines[i];
        if (qline.trim() === "") {
          if (para.length > 0) {
            paras.push(para.join("<br>"));
            para = [];
          }
          i += 1;
          continue;
        }
        var qmatch = qline.match(/^\s*>\s?(.*)$/);
        if (!qmatch) {
          break;
        }
        para.push(renderInline(escapeHtml(qmatch[1].trim())));
        i += 1;
      }
      if (para.length > 0) {
        paras.push(para.join("<br>"));
      }
      html.push("<blockquote>");
      paras.forEach(function (paragraph) {
        html.push("<p>" + paragraph + "</p>");
      });
      html.push("</blockquote>");
      continue;
    }

    var paragraph = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !startsBlock(lines, i)
    ) {
      paragraph.push(renderInline(escapeHtml(lines[i].trim())));
      i += 1;
    }
    if (paragraph.length > 0) {
      html.push("<p>" + paragraph.join("<br>") + "</p>");
    }
  }
  return html.join("\n");
}

function isTerminal(status) {
  return status === "done" || status === "failed" || status === "cancelled";
}
