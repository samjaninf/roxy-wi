$(function () {
    const dialog = $('#change-create-dialog');
    if (!dialog.length) {
        return;
    }
    const i18n = $('#change-editor-i18n').data();

    function roleLabel(role) {
        const key = 'role' + role.charAt(0).toUpperCase() + role.slice(1);
        return i18n[key] || role;
    }

    function renderRolloutPreview(targets) {
        const container = $('#change-rollout-preview').empty();
        const table = $('<table class="overview compact change-rollout-preview-table">');
        const head = $('<tr>')
            .append($('<th>').text(i18n.node))
            .append($('<th>').text(i18n.role))
            .append($('<th>').text(i18n.canary))
            .append($('<th>').text(i18n.exclude));
        const body = $('<tbody>');
        (targets || []).forEach(function (target) {
            const canary = $('<input type="checkbox" class="change-target-canary">')
                .attr('data-server-id', target.server_id)
                .prop('disabled', !target.canary_eligible);
            const excluded = $('<input type="checkbox" class="change-target-excluded">')
                .attr('data-server-id', target.server_id)
                .prop('disabled', !target.can_exclude);
            canary.on('change', function () {
                if (this.checked) {
                    excluded.prop('checked', false);
                }
            });
            excluded.on('change', function () {
                if (this.checked) {
                    canary.prop('checked', false);
                }
            });
            body.append(
                $('<tr>')
                    .append($('<td>').text(target.server_name + ' (' + target.server_ip + ')'))
                    .append($('<td>').text(roleLabel(target.role)))
                    .append($('<td>').append(canary))
                    .append($('<td>').append(excluded))
            );
        });
        table.append($('<thead>').append(head)).append(body);
        container.append(table);
    }

    function loadRolloutPreview() {
        const container = $('#change-rollout-preview').text(i18n.rolloutLoading);
        const form = $('#saveconfig');
        $.getJSON('/changes/api/rollout-preview', {
            server_id: $('#serv').val(),
            service: form.find('[name="service"]').val()
        }).done(function (response) {
            renderRolloutPreview(response.data || []);
        }).fail(function () {
            container.text(i18n.rolloutUnavailable);
        });
    }

    function renderNotificationDestinations(destinations) {
        const container = $('#change-notification-destinations').empty();
        if (!destinations.length) {
            container.append($('<span>').text(i18n.notificationEmpty + ' '));
            container.append(
                $('<a href="/channel">').text(i18n.configureNotifications)
            );
            return;
        }
        const grouped = {};
        destinations.forEach(function (destination) {
            if (!grouped[destination.channel]) {
                grouped[destination.channel] = {
                    label: destination.channel_label,
                    items: []
                };
            }
            grouped[destination.channel].items.push(destination);
        });
        Object.keys(grouped).forEach(function (channel) {
            const group = grouped[channel];
            const section = $('<div class="change-notification-group">');
            section.append($('<strong>').text(group.label));
            const recipients = $('<div class="change-notification-recipient-list">');
            group.items.forEach(function (destination) {
                const checkbox = $('<input type="checkbox" class="change-notification-destination">')
                    .attr('data-channel', destination.channel)
                    .attr('data-recipient-id', destination.recipient_id);
                const text = $('<span>')
                    .append($('<span class="change-notification-recipient-name">').text(destination.label));
                if (destination.destination && destination.destination !== destination.label) {
                    text.append(
                        $('<small class="change-notification-recipient-address">')
                            .text(destination.destination)
                    );
                }
                recipients.append($('<label>').append(checkbox, text));
            });
            container.append(section.append(recipients));
        });
    }

    function loadNotificationDestinations() {
        const container = $('#change-notification-destinations').text(i18n.notificationLoading);
        $.getJSON('/changes/api/notification-destinations').done(function (response) {
            renderNotificationDestinations(response.data || []);
        }).fail(function () {
            container.empty()
                .append($('<span>').text(i18n.notificationUnavailable + ' '))
                .append($('<button type="button" class="btn btn-default">')
                    .text(i18n.notificationRetry)
                    .on('click', loadNotificationDestinations));
        });
    }

    function selectedTargetIds(selector) {
        return $(selector + ':checked').map(function () {
            return Number($(this).data('server-id'));
        }).get();
    }

    function createChange() {
        const title = $('#change-title').val().trim();
        if (!title) {
            toastr.warning(i18n.required);
            return;
        }
        if (typeof myCodeMirror !== 'undefined') {
            myCodeMirror.save();
        }
        const form = $('#saveconfig');
        const batchSize = $('#change-batch-size').val();
        const payload = {
            server_id: $('#serv').val(),
            service: form.find('[name="service"]').val(),
            action: $('#change-action').val(),
            execution_mode: $('#change-execution-mode').val(),
            batch_size: batchSize ? Number(batchSize) : null,
            max_parallel: Number($('#change-max-parallel').val()),
            manual_promotion: $('#change-manual-promotion').is(':checked'),
            health_check_mode: $('#change-health-mode').val(),
            health_check_retries: Number($('#change-health-retries').val()),
            health_check_interval: Number($('#change-health-interval').val()),
            canary_server_ids: selectedTargetIds('.change-target-canary'),
            excluded_server_ids: selectedTargetIds('.change-target-excluded'),
            notification_destinations: $('.change-notification-destination:checked').map(function () {
                return {
                    channel: String($(this).data('channel')),
                    recipient_id: Number($(this).data('recipient-id'))
                };
            }).get(),
            config: form.find('[name="config"]').val(),
            file_path: form.find('[name="file_path"]').val(),
            title: title,
            description: $('#change-description').val().trim() || null,
            requires_approval: $('#change-requires-approval').is(':checked')
        };
        $.ajax({
            url: '/changes/api',
            method: 'POST',
            contentType: 'application/json; charset=UTF-8',
            dataType: 'json',
            data: JSON.stringify(payload),
            beforeSend: function () { toastr.clear(); },
            success: function (response) {
				if (window.RoxywiConfigWorkflow) {
					window.RoxywiConfigWorkflow.persisted();
				}
                window.location.assign('/changes');
            }
        });
    }

    dialog.dialog({
        autoOpen: false,
        modal: true,
        width: Math.min(780, window.innerWidth - 30),
        buttons: [
            {text: i18n.create, click: createChange},
            {text: i18n.cancel, click: function () { dialog.dialog('close'); }}
        ]
    });

    $('#open-change-dialog').on('click', function () {
        loadRolloutPreview();
        loadNotificationDestinations();
        dialog.dialog('open');
    });
});
