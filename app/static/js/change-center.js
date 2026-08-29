$(function () {
    const root = $('#change-center');
    const tableBody = $('#change-center-table tbody');
    const i18n = $('#change-center-i18n').data();
    const currentUserId = Number(root.data('user-id'));
    const currentRole = Number(root.data('role'));
    let changesById = {};
    let actionPoll = null;
    let openDetailsId = null;
    let iconRefreshScheduled = false;

    function textCell(value) {
        return $('<td>').text(value == null ? '' : value);
    }

    function statusLabel(status) {
        const statusKey = status.replace(/_([a-z])/g, function (_match, letter) { return letter.toUpperCase(); });
        const translated = i18n[statusKey] || status.replaceAll('_', ' ');
        return $('<span>').addClass('change-status change-status-' + status).text(translated);
    }

    function actionButton(icon, title, action, changeId) {
        return $('<button type="button" class="ui-button ui-widget ui-corner-all">')
            .addClass('change-action-' + action)
            .attr('title', title)
            .attr('aria-label', title)
            .append($('<span aria-hidden="true">').addClass('fas ' + icon))
            .on('click', function () { runAction(changeId, action); });
    }

    function refreshActionIcons() {
        if (window.FontAwesome && window.FontAwesome.dom && window.FontAwesome.dom.i2svg) {
            window.FontAwesome.dom.i2svg({node: tableBody.get(0)});
			return;
        }
		if (!iconRefreshScheduled) {
			iconRefreshScheduled = true;
			window.addEventListener('load', function () {
				iconRefreshScheduled = false;
				refreshActionIcons();
			}, {once: true});
		}
    }

    function renderActions(change) {
        const actions = $('<td>').addClass('change-actions');
        actions.append(actionButton('fa-eye', i18n.details, 'details', change.id));
        if (['draft', 'validation_failed'].includes(change.status)) {
            actions.append(actionButton('fa-check', i18n.validate, 'validate', change.id));
        }
        if (change.status === 'pending_approval' && currentRole <= 2 && change.user_id !== currentUserId) {
            actions.append(actionButton('fa-user-check', i18n.approve, 'approve', change.id));
        }
        if (['validated', 'approved', 'auto_rolled_back', 'auto_rollback_failed', 'rollback_failed', 'deployment_interrupted'].includes(change.status)) {
            actions.append(actionButton('fa-rocket', i18n.deploy, 'deploy', change.id));
        }
        if (['deployed', 'rollback_failed', 'auto_rollback_failed', 'deployment_interrupted'].includes(change.status)) {
            actions.append(actionButton('fa-undo', i18n.rollback, 'rollback', change.id));
        }
        if (['draft', 'validation_failed', 'validated', 'pending_approval', 'approved'].includes(change.status)) {
            actions.append(actionButton('fa-times', i18n.cancel, 'cancel', change.id));
        }
        if (change.recoverable) {
            actions.append(actionButton('fa-unlock-alt', i18n.recover, 'recover', change.id));
        }
        return actions;
    }

    function render(changes) {
        changesById = {};
        tableBody.empty();
        changes.forEach(function (change) {
            changesById[change.id] = change;
            const row = $('<tr>');
            row.append(textCell(change.id));
            row.append(textCell(change.title));
            row.append(textCell(change.service));
            row.append(textCell((change.server_name || '') + (change.server_ip ? ' (' + change.server_ip + ')' : '')));
            row.append($('<td>').append(statusLabel(change.status)));
            row.append(textCell(change.created_by));
            row.append(textCell(change.created_at ? new Date(change.created_at).toLocaleString() : ''));
            row.append(renderActions(change));
            tableBody.append(row);
        });
        refreshActionIcons();
        if (openDetailsId && changesById[openDetailsId] && $('#change-details-dialog').dialog('isOpen')) {
            renderDetailsContent(changesById[openDetailsId]);
        }
        $('#change-center-empty').toggle(changes.length === 0);
        $('#change-center-table').toggle(changes.length !== 0);
    }

    function loadChanges() {
        $.getJSON('/changes/api')
            .done(function (response) { render(response.data || []); });
    }

    function renderDiff(diff) {
        const container = $('#change-details-diff').empty();
        if (!diff) {
            container.text('—');
            return;
        }
        diff.replace(/\r\n/g, '\n').split('\n').forEach(function (content) {
            let lineType = 'context';
            if (content.startsWith('+++') || content.startsWith('---')) {
                lineType = 'header';
            } else if (content.startsWith('@@')) {
                lineType = 'hunk';
            } else if (content.startsWith('+')) {
                lineType = 'added';
            } else if (content.startsWith('-')) {
                lineType = 'removed';
            }
            container.append(
                $('<span>')
                    .addClass('change-diff-line change-diff-' + lineType)
                    .text(content || '\u00a0')
            );
        });
    }

    function targetOutput(target) {
        const output = target.rollback_output || target.deployment_output || target.validation_output;
        return normalizeRoxywiResponse(output) || '—';
    }

    function renderRollout(targets) {
        const body = $('#change-details-rollout tbody').empty();
        (targets || []).forEach(function (target) {
            const roleKey = 'role' + target.role.charAt(0).toUpperCase() + target.role.slice(1);
            const row = $('<tr>');
            row.append(textCell(target.server_name + ' (' + target.server_ip + ')'));
            row.append(textCell(i18n[roleKey] || target.role));
            row.append($('<td>').append(statusLabel(target.status)));
            row.append($('<td>').append($('<pre>').text(targetOutput(target))));
            body.append(row);
        });
        if (!targets || targets.length === 0) {
            body.append($('<tr>').append($('<td colspan="4">').text('—')));
        }
    }

    function renderDetailsContent(change) {
        const executionMode = change.execution_mode || 'rolling';
        const executionModeKey = 'executionMode' + executionMode.charAt(0).toUpperCase() + executionMode.slice(1);
        $('#change-details-summary').empty()
            .append($('<p>').text('#' + change.id + ' — ' + change.title))
            .append($('<p>').text(change.service + ' / ' + (change.server_name || change.server_id) + ' / ' + change.remote_path))
            .append($('<p>').text(i18n.executionMode + ': ' + (i18n[executionModeKey] || executionMode)))
            .append($('<p>').text(change.description || ''));
        renderRollout(change.targets);
        renderDiff(change.diff);
        $('#change-details-validation').text(normalizeRoxywiResponse(change.validation_output) || '—');
        $('#change-details-deployment').text(normalizeRoxywiResponse(change.deployment_output) || '—');
        $('#change-details-rollback').text(normalizeRoxywiResponse(change.rollback_output) || '—');
    }

    function showDetails(change) {
        openDetailsId = change.id;
        renderDetailsContent(change);
        $('#change-details-dialog').dialog({
            modal: true,
            width: '80%',
            maxHeight: 800,
            close: function () { openDetailsId = null; }
        }).dialog('open');
    }

    function confirmAction(action, message, callback) {
        const titles = {
            deploy: i18n.deploy,
            rollback: i18n.rollback,
            cancel: i18n.cancel,
            recover: i18n.recover
        };
        const dialog = $('#change-confirm-dialog');
        $('#change-confirm-message').text(message);
        dialog.dialog({
            autoOpen: false,
            modal: true,
            resizable: false,
            width: 450,
            title: titles[action],
            buttons: [
                {
                    text: i18n.confirm,
                    class: 'change-confirm-button change-confirm-' + action,
                    click: function () {
                        dialog.dialog('close');
                        callback();
                    }
                },
                {
                    text: i18n.cancel,
                    click: function () { dialog.dialog('close'); }
                }
            ]
        }).dialog('open');
    }

    function executeAction(changeId, action) {
        $.ajax({
            url: '/changes/api/' + changeId + '/' + action,
            method: 'POST',
            dataType: 'json',
            beforeSend: function () {
                toastr.clear();
                window.clearInterval(actionPoll);
                actionPoll = window.setInterval(loadChanges, 1500);
            },
            success: function () {
                const messages = {
                    validate: i18n.validated,
                    approve: i18n.approved,
                    deploy: i18n.deployed,
                    rollback: i18n.rolledBack,
                    cancel: i18n.cancelled,
                    recover: i18n.recovered
                };
                toastr.success(messages[action] || i18n.operationSuccess);
                loadChanges();
            },
            error: function () {
                loadChanges();
            },
            complete: function () {
                window.clearInterval(actionPoll);
                actionPoll = null;
            }
        });
    }

    function runAction(changeId, action) {
        if (action === 'details') {
            showDetails(changesById[changeId]);
            return;
        }
        const confirmations = {
            deploy: i18n.confirmDeploy,
            rollback: i18n.confirmRollback,
            cancel: i18n.confirmCancel,
            recover: i18n.confirmRecover
        };
        if (confirmations[action]) {
            confirmAction(action, confirmations[action], function () {
                executeAction(changeId, action);
            });
            return;
        }
        executeAction(changeId, action);
    }

    loadChanges();
    window.setInterval(loadChanges, 60000);
});
