(function () {
  "use strict";

  var tbody = document.getElementById("leaderboard-body");
  var table = document.getElementById("leaderboard-table");
  var currentSort = { key: "stars", dir: "desc" };
  var rows = [];

  function formatStars(n) {
    if (n === null || n === undefined) return "?";
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(n);
  }

  function formatDate(iso) {
    if (!iso) return "?";
    return iso.slice(0, 10);
  }

  function gradeCell(percent, grade) {
    return '<span class="grade grade-' + grade + '">' + grade + "</span> " +
      '<span class="text-dim">' + percent + "%</span>";
  }

  function render() {
    var sorted = rows.slice().sort(function (a, b) {
      var key = currentSort.key;
      var av = a[key], bv = b[key];
      if (av === null || av === undefined) av = -Infinity;
      if (bv === null || bv === undefined) bv = -Infinity;
      if (typeof av === "string") { av = av.toLowerCase(); bv = bv.toLowerCase(); }
      var cmp = av < bv ? -1 : av > bv ? 1 : 0;
      return currentSort.dir === "asc" ? cmp : -cmp;
    });

    if (sorted.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="loading">No scan data yet — check back after the next run.</td></tr>';
      return;
    }

    tbody.innerHTML = sorted.map(function (r) {
      return "<tr>" +
        '<td><a class="repo-link" href="' + r.url + '" target="_blank" rel="noopener">' + r.repo + "</a></td>" +
        "<td>" + r.language + "</td>" +
        '<td class="numeric">' + formatStars(r.stars) + "</td>" +
        '<td class="numeric">' + r.tool_count + "</td>" +
        '<td class="numeric">' + gradeCell(r.quality_percent, r.quality_grade) + "</td>" +
        '<td class="numeric">' + gradeCell(r.security_percent, r.security_grade) + "</td>" +
        "<td>" + formatDate(r.last_scanned) + "</td>" +
        "</tr>";
    }).join("");

    table.querySelectorAll("th").forEach(function (th) {
      th.classList.remove("sorted", "asc");
      if (th.dataset.sort === currentSort.key) {
        th.classList.add("sorted");
        if (currentSort.dir === "asc") th.classList.add("asc");
      }
    });
  }

  table.querySelectorAll("th[data-sort]").forEach(function (th) {
    th.addEventListener("click", function () {
      var key = th.dataset.sort;
      if (currentSort.key === key) {
        currentSort.dir = currentSort.dir === "asc" ? "desc" : "asc";
      } else {
        currentSort.key = key;
        currentSort.dir = "desc";
      }
      render();
    });
  });

  fetch("./data.json")
    .then(function (res) { return res.json(); })
    .then(function (data) {
      rows = data;
      render();
    })
    .catch(function () {
      tbody.innerHTML = '<tr><td colspan="7" class="loading">Couldn\'t load scan data.</td></tr>';
    });
})();
