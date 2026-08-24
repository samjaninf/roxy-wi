$(function () {
    const dialog = $('#change-create-dialog');
    if (!dialog.length) {
        return;
    }
    const i18n = $('#change-editor-i18n').data();

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
        const payload = {
            server_id: $('#serv').val(),
            service: form.find('[name="service"]').val(),
            action: $('#change-action').val(),
            execution_mode: $('#change-execution-mode').val(),
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
                dialog.dialog('close');
                toastr.success(i18n.created + ' #' + response.data.id);
            }
        });
    }

    dialog.dialog({
        autoOpen: false,
        modal: true,
        width: 620,
        buttons: [
            {text: i18n.create, click: createChange},
            {text: i18n.cancel, click: function () { dialog.dialog('close'); }}
        ]
    });

    $('#open-change-dialog').on('click', function () {
        dialog.dialog('open');
    });
});
