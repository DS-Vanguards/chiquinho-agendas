(function () {
    "use strict";
    var POLL_MS = 2500;

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function limitVisibleRows(wrap, visible) {
        if (!wrap) return;
        var table = wrap.querySelector(".data-table");
        if (!table || !table.tHead || !table.tBodies[0]) {
            wrap.style.maxHeight = "";
            return;
        }
        var rows = table.tBodies[0].rows;
        if (rows.length <= visible) {
            wrap.style.maxHeight = "";
            return;
        }
        var height = table.tHead.offsetHeight;
        for (var i = 0; i < visible; i++) {
            height += rows[i].offsetHeight;
        }
        wrap.style.maxHeight = height + "px";
    }

    function fetchJson(url) {
        return fetch(url, {
            headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
            credentials: "same-origin",
        }).then(function (res) {
            var type = (res.headers.get("content-type") || "").toLowerCase();
            if (!res.ok || type.indexOf("json") === -1) return null;
            return res.json();
        }).catch(function () {
            return null;
        });
    }

    function startPolling(urlBuilder, onData) {
        var rev = "";
        var timer = null;
        var inFlight = false;

        function tick() {
            if (document.hidden || inFlight) return;
            inFlight = true;
            fetchJson(urlBuilder(rev)).then(function (data) {
                inFlight = false;
                if (!data) return;
                if (data.unchanged) {
                    if (data.revision) rev = data.revision;
                    return;
                }
                var applied = onData(data);
                if (applied !== false && data.revision) rev = data.revision;
            }).catch(function () {
                inFlight = false;
            });
        }

        timer = setInterval(tick, POLL_MS);
        setTimeout(tick, 800);
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden) tick();
        });
        return timer;
    }

    function bookingRowHtml(item) {
        return (
            "<tr>" +
            "<td><strong>" + escapeHtml(item.room_label || item.room) + "</strong></td>" +
            "<td><strong>" + escapeHtml(item.date_label) + "</strong></td>" +
            "<td><strong>" + escapeHtml(item.start_time) + "</strong></td>" +
            "<td><strong>" + escapeHtml(item.end_time) + "</strong></td>" +
            "<td><strong>" + escapeHtml(item.username) + "</strong></td>" +
            "<td><span class=\"badge badge-status badge-" + escapeHtml(item.status) + "\">" +
            escapeHtml(item.status) + "</span></td>" +
            "</tr>"
        );
    }

    function detailedTableHtml(items) {
        if (!items || !items.length) {
            return "<p class=\"empty-state\">Nenhum agendamento encontrado para este turno nesta data.</p>";
        }
        var rows = items.map(bookingRowHtml).join("");
        return (
            "<div class=\"table-wrapper detailed-bookings-scroll\" data-visible-rows=\"4\">" +
            "<table class=\"data-table\"><thead><tr>" +
            "<th>Sala</th><th>Data</th><th>Início</th><th>Fim</th><th>Professor</th><th>Status</th>" +
            "</tr></thead><tbody>" + rows + "</tbody></table></div>"
        );
    }

    function renderDetailed(data) {
        var map = {
            manha: data.bookings_manha || [],
            tarde: data.bookings_tarde || [],
        };
        document.querySelectorAll("[data-live-shift]").forEach(function (section) {
            var shift = section.getAttribute("data-live-shift");
            var body = section.querySelector(".detailed-shift-body");
            if (!body) return;
            body.innerHTML = detailedTableHtml(map[shift] || []);
            limitVisibleRows(body.querySelector(".detailed-bookings-scroll"), 4);
        });
    }

    function adminModalOpen() {
        return !!document.querySelector(".modal-overlay:not([hidden])");
    }

    function optionHtml(status, selected) {
        return (
            "<option value=\"" + escapeHtml(status) + "\"" +
            (status === selected ? " selected" : "") + ">" +
            escapeHtml(status) + "</option>"
        );
    }

    function renderAdminBookings(data) {
        var host = document.querySelector("[data-live-admin-bookings]");
        if (!host) return;
        var items = data.bookings || [];
        var isAdmin = !!data.is_admin;
        var statuses = data.statuses || [];
        var html;
        if (!items.length) {
            html = "<p class=\"empty-state\">Nenhum agendamento registrado.</p>";
        } else {
            html = "<div class=\"table-wrapper admin-bookings-scroll\"><table class=\"data-table\"><thead><tr>" +
                "<th>Sala</th><th>Data</th><th>Horário</th><th>Professor</th><th>Status</th>" +
                (isAdmin ? "<th>Ações</th>" : "") +
                "</tr></thead><tbody>";
            items.forEach(function (item) {
                html += "<tr><td>" + escapeHtml(item.room_label || item.room) + "</td>" +
                    "<td>" + escapeHtml(item.date_label) + "</td>" +
                    "<td>" + escapeHtml(item.start_time) + " — " + escapeHtml(item.end_time) + "</td>" +
                    "<td>" + escapeHtml(item.username) + "</td>" +
                    "<td><span class=\"badge badge-status badge-" + escapeHtml(item.status) + "\">" +
                    escapeHtml(item.status) + "</span></td>";
                if (isAdmin) {
                    html += "<td class=\"actions-cell\">" +
                        "<form method=\"POST\" action=\"/admin/booking/" + item.id + "/status\" class=\"inline-form\">" +
                        "<select name=\"status\">" +
                        statuses.map(function (status) { return optionHtml(status, item.status); }).join("") +
                        "</select>" +
                        "<button type=\"submit\" class=\"btn btn-sm btn-secondary\">Alterar</button></form>" +
                        "<form method=\"POST\" action=\"/admin/booking/" + item.id + "/delete\" class=\"inline-form\" " +
                        "onsubmit=\"return confirm('Excluir este agendamento?');\">" +
                        "<button type=\"submit\" class=\"btn btn-sm btn-danger\">Excluir</button></form></td>";
                }
                html += "</tr>";
            });
            html += "</tbody></table></div>";
        }
        host.innerHTML = html;
        limitVisibleRows(host.querySelector(".admin-bookings-scroll"), 6);
    }

    function renderAdminUsers(data) {
        var tbody = document.querySelector("[data-live-admin-users] tbody");
        if (!tbody) return;
        var isAdmin = !!data.is_admin;
        tbody.innerHTML = (data.users || []).map(function (user) {
            var shiftClass = user.shift || "none";
            var actions = "";
            if (user.can_edit) {
                actions +=
                    "<button type=\"button\" class=\"btn btn-sm btn-secondary js-open-role-panel\" " +
                    "data-user-id=\"" + user.id + "\" data-username=\"" + escapeHtml(user.username) +
                    "\" data-role=\"" + escapeHtml(user.role) + "\">Cargo</button>";
            }
            if (isAdmin) {
                actions +=
                    "<button type=\"button\" class=\"btn btn-sm btn-secondary js-open-shift-panel\" " +
                    "data-user-id=\"" + user.id + "\" data-username=\"" + escapeHtml(user.username) +
                    "\" data-shift=\"" + escapeHtml(user.shift) + "\">Turno</button>";
            }
            if (user.can_reset) {
                actions +=
                    "<form method=\"POST\" action=\"/admin/users/" + user.id + "/reset-password\" class=\"inline-form\" " +
                    "data-username=\"" + escapeHtml(user.username) + "\" " +
                    "onsubmit=\"return confirm('Redefinir senha de ' + this.dataset.username + '?');\">" +
                    "<button type=\"submit\" class=\"btn btn-sm btn-warning\">Redefinir senha</button></form>";
            }
            if (user.can_edit) {
                actions +=
                    "<form method=\"POST\" action=\"/admin/users/" + user.id + "/delete\" class=\"inline-form\" " +
                    "data-username=\"" + escapeHtml(user.username) + "\" " +
                    "onsubmit=\"return confirm('Tem certeza que deseja EXCLUIR permanentemente o usuário ' + this.dataset.username + '? Todos os agendamentos dele serão apagados.');\">" +
                    "<button type=\"submit\" class=\"btn btn-sm btn-danger\">Excluir usuário</button></form>";
            }
            return (
                "<tr><td>" + escapeHtml(user.username) + "</td>" +
                "<td>" + escapeHtml(user.email) + "</td>" +
                "<td><span class=\"badge badge-" + escapeHtml(user.role) + "\">" +
                escapeHtml(user.role_label) + "</span></td>" +
                "<td><span class=\"badge badge-shift badge-shift-" + escapeHtml(shiftClass) + "\">" +
                escapeHtml(user.shift_label) + "</span></td>" +
                "<td class=\"actions-cell\">" + actions + "</td></tr>"
            );
        }).join("");
    }

    function renderAdminMachines(data) {
        var host = document.querySelector("[data-live-admin-machines]");
        if (!host) return;
        var items = data.machines || [];
        if (!items.length) {
            host.innerHTML = "<p class=\"text-muted\">Nenhuma máquina bloqueada no momento.</p>";
            return;
        }
        var rows = items.map(function (machine) {
            var badge = machine.lock_level >= 2
                ? "<span class=\"badge badge-status badge-bloqueado\">1 dia</span>"
                : "<span class=\"badge badge-status badge-agendado\">5 min</span>";
            return (
                "<tr><td><code title=\"" + escapeHtml(machine.token) + "\">" +
                escapeHtml(machine.token_short) + "</code></td>" +
                "<td>" + escapeHtml(machine.ip_address) + "</td>" +
                "<td>" + badge + "</td>" +
                "<td>" + escapeHtml(machine.locked_until) + "</td>" +
                "<td class=\"actions-cell\">" +
                "<button type=\"button\" class=\"btn btn-sm btn-secondary js-open-lock-panel\" " +
                "data-lock-id=\"" + machine.id + "\" data-lock-token=\"" +
                escapeHtml(machine.token_short) + "\">Alterar bloqueio</button>" +
                "<form method=\"POST\" action=\"/admin/machine-locks/" + machine.id + "/remove\" class=\"inline-form\" " +
                "onsubmit=\"return confirm('Remover o bloqueio desta máquina?');\">" +
                "<button type=\"submit\" class=\"btn btn-sm btn-danger\">Remover bloqueio</button></form>" +
                "</td></tr>"
            );
        }).join("");
        host.innerHTML =
            "<div class=\"table-wrapper admin-users-scroll\"><table class=\"data-table\">" +
            "<thead><tr><th>Token</th><th>IP</th><th>Tipo</th><th>Bloqueado até</th><th>Ações</th></tr></thead>" +
            "<tbody>" + rows + "</tbody></table></div>";
    }

    function initAgendaLive() {
        var root = document.querySelector("[data-live-agenda]");
        if (!root) return;
        var dateInput = document.querySelector(".date-input");
        var date = (dateInput && dateInput.value) || root.getAttribute("data-live-date") || "";
        startPolling(function (rev) {
            var url = "/live/agenda?date=" + encodeURIComponent(date);
            if (rev) url += "&rev=" + encodeURIComponent(rev);
            return url;
        }, function (data) {
            var handlers = (window.ScheduleLive && window.ScheduleLive.handlers) || [];
            handlers.forEach(function (fn) {
                try { fn(data.bookings || []); } catch (err) {}
            });
            if (document.querySelector("[data-live-detailed]")) {
                renderDetailed(data);
            }
        });
        document.querySelectorAll(".detailed-bookings-scroll").forEach(function (wrap) {
            limitVisibleRows(wrap, 4);
        });
    }

    function initAdminLive() {
        if (!document.querySelector("[data-live-admin-users], [data-live-admin-machines], [data-live-admin-bookings]")) {
            return;
        }
        startPolling(function (rev) {
            var url = "/live/admin";
            if (rev) url += "?rev=" + encodeURIComponent(rev);
            return url;
        }, function (data) {
            if (adminModalOpen()) return false;
            renderAdminBookings(data);
            renderAdminUsers(data);
            renderAdminMachines(data);
        });
        limitVisibleRows(document.querySelector(".admin-bookings-scroll"), 6);
    }

    function prefetchNav() {
        document.querySelectorAll(".nav-links a[href]").forEach(function (link) {
            link.addEventListener("pointerenter", function () {
                if (link.dataset.prefetched) return;
                var href = link.getAttribute("href");
                if (!href) return;
                link.dataset.prefetched = "1";
                fetch(href, {
                    credentials: "same-origin",
                    headers: { "X-Requested-With": "XMLHttpRequest" },
                }).catch(function () {});
            }, { once: true });
        });
    }

    initAgendaLive();
    initAdminLive();
    prefetchNav();
})();
