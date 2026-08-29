(function (window, $) {
	'use strict';

	$(function () {
		const form = $('#saveconfig');
		if (!form.length || typeof window.myCodeMirror === 'undefined') return;
		const editor = window.myCodeMirror;
		const i18n = $('#config-workflow-i18n').data();
		const original = editor.getValue();
		const draftKey = [
			'roxywi-config-draft',
			form.find('[name="service"]').val(),
			$('#serv').val(),
			form.find('[name="file_path"]').val() || 'main'
		].join(':');
		let dirty = false;
		let saveTimer = null;
		let allowSubmit = false;

		function setDirty(value) {
			dirty = value;
			$('#config-editor-state')
				.toggleClass('is-saved', !dirty)
				.toggleClass('is-dirty', dirty);
			$('#config-dirty-icon').text(dirty ? '●' : '✓');
			$('#config-dirty-state').text(dirty ? i18n.unsaved : i18n.saved);
		}

		function readDraft() {
			try { return JSON.parse(localStorage.getItem(draftKey)); } catch (error) { return null; }
		}

		function removeDraft() {
			try { localStorage.removeItem(draftKey); } catch (error) { /* Storage is optional. */ }
			$('#config-draft-banner').prop('hidden', true);
		}

		function saveDraft() {
			const content = editor.getValue();
			setDirty(content !== original);
			if (!dirty) {
				removeDraft();
				return;
			}
			try {
				localStorage.setItem(draftKey, JSON.stringify({content: content, savedAt: new Date().toISOString()}));
			} catch (error) {
				// Editing must continue when storage is unavailable or full.
			}
		}

		function unifiedDiff(before, after) {
			if (before === after) return '';
			const oldLines = before.replace(/\r/g, '').split('\n');
			const newLines = after.replace(/\r/g, '').split('\n');
			return [
				'--- running.conf',
				'+++ edited.conf',
				'@@ -1,' + oldLines.length + ' +1,' + newLines.length + ' @@'
			].concat(oldLines.map(function (line) { return '-' + line; }))
				.concat(newLines.map(function (line) { return '+' + line; })).join('\n');
		}

		function renderReview() {
			const target = document.getElementById('config-review-diff');
			target.innerHTML = '';
			const diff = unifiedDiff(original, editor.getValue());
			if (!diff) {
				$('<div class="rw-empty-state"></div>').text(i18n.noChanges).appendTo(target);
				return;
			}
			if (window.Diff2HtmlUI) {
				const ui = new Diff2HtmlUI(target, diff, {drawFileList: false, matching: 'lines', outputFormat: 'side-by-side'});
				ui.draw();
			} else {
				$('<pre class="rw-notification-raw"></pre>').text(diff).appendTo(target);
			}
		}

		function submitAfterReview(submitter) {
			allowSubmit = true;
			editor.save();
			if (form[0].requestSubmit) form[0].requestSubmit(submitter); else submitter.click();
		}

		function validateCandidate() {
			editor.save();
			const result = $('#config-validation-result').prop('hidden', false)
				.removeClass('rw-error-state').addClass('rw-loading-state')
				.empty().append($('<b></b>').text(i18n.validation || 'Validation')).append('…');
			$.ajax({
				url: form.attr('action') + '/validate',
				type: 'POST',
				contentType: 'application/json; charset=utf-8',
				data: JSON.stringify({
					config: editor.getValue(),
					file_path: form.find('[name="file_path"]').val() || null
				}),
				suppressGlobalError: true,
				success: function (response) {
					result.removeClass('rw-loading-state rw-error-state').addClass('rw-operation-result').empty();
					result.append($('<b></b>').text(i18n.validation || 'Validation')).append($('<pre></pre>').text(response.data || 'OK'));
				},
				error: function (xhr) {
					const parsed = RoxywiUI.parseResponse(xhr.responseJSON || xhr.responseText);
					result.removeClass('rw-loading-state rw-operation-result').addClass('rw-error-state').empty();
					result.append($('<b></b>').text(i18n.validation || 'Validation')).append($('<pre></pre>').text(parsed.summary));
				}
			});
		}

		form.on('submit', function (event) {
			const submitter = event.originalEvent && event.originalEvent.submitter;
			const action = submitter ? submitter.value : 'save';
			editor.save();
			if (allowSubmit) {
				allowSubmit = false;
				return;
			}
			if (action === 'test') {
				event.preventDefault();
				validateCandidate();
				return;
			}
			if (!submitter || ['save', 'reload', 'restart'].indexOf(action) === -1) return;
			event.preventDefault();
			renderReview();
			$('#config-review-dialog').dialog({
				modal: true,
				width: Math.min(980, Math.max(340, window.innerWidth - 32)),
				classes: {'ui-dialog': 'rw-dialog-responsive'},
				buttons: [{
					text: i18n.apply,
					click: function () { $(this).dialog('close'); submitAfterReview(submitter); }
				}, {
					text: i18n.cancel,
					click: function () { $(this).dialog('close'); }
				}]
			});
		});

		editor.on('change', function () {
			window.clearTimeout(saveTimer);
			setDirty(editor.getValue() !== original);
			saveTimer = window.setTimeout(saveDraft, 500);
		});

		const draft = readDraft();
		if (draft && draft.content && draft.content !== original) $('#config-draft-banner').prop('hidden', false);
		else if (draft) removeDraft();

		$('#config-restore-draft').on('click', function () {
			const savedDraft = readDraft();
			if (savedDraft && typeof savedDraft.content === 'string') editor.setValue(savedDraft.content);
			$('#config-draft-banner').prop('hidden', true);
		});
		$('#config-discard-draft').on('click', function () { removeDraft(); });

		window.addEventListener('beforeunload', function (event) {
			if (!dirty) return;
			event.preventDefault();
			event.returnValue = i18n.leaveWarning;
			return i18n.leaveWarning;
		});

		window.RoxywiConfigWorkflow = {
			persisted: function () { removeDraft(); setDirty(false); }
		};
	});
})(window, window.jQuery);
