/**
 * GwDatatable — shared behaviour for the server-side .gw-dt tables.
 *
 *   GwDatatable.init({
 *     toolbar: '#leavesDtForm',            // GET form: auto-submit selects, debounced search
 *     bulkForm: '#bulkForm',               // POST form holding the bulk bar (optional)
 *     actionInput: '#bulkActionInput',     // hidden input receiving the action name (optional)
 *     actions: { delete: { title: n => '...', message, note, confirmText, variant, icon } },
 *   });
 *
 * Rows with data-href open that URL when clicked (or Enter) unless the click lands on a control.
 * Row checkboxes: [data-row-check][data-label]. Single-row delete buttons: [data-row-delete=id][data-label].
 */
(function () {
    'use strict';

    const INTERACTIVE = 'a, button, input, select, textarea, label, .dropdown, .dropdown-menu, [data-no-row-click]';

    function initRowClick(root, bulk) {
        // Checkbox cell: a click anywhere in the cell toggles the box and never opens the record.
        root.querySelectorAll('td.gw-dt-check').forEach(td => {
            td.addEventListener('click', e => {
                e.stopPropagation();
                if (e.target.matches('input')) return; // the checkbox handles itself
                const box = td.querySelector('input[type="checkbox"]');
                if (box && !box.disabled) {
                    box.checked = !box.checked;
                    box.dispatchEvent(new Event('change', { bubbles: true }));
                }
            });
        });

        root.querySelectorAll('tr[data-href]').forEach(tr => {
            tr.addEventListener('click', e => {
                if (e.target.closest(INTERACTIVE)) return;
                if (window.getSelection && String(window.getSelection())) return; // allow text selection
                // While rows are selected, clicking a row adds / removes it instead of opening it.
                if (bulk && bulk.hasSelection()) {
                    bulk.toggleRow(tr);
                    return;
                }
                if (e.metaKey || e.ctrlKey) window.open(tr.dataset.href, '_blank');
                else window.location.href = tr.dataset.href;
            });
            tr.addEventListener('keydown', e => {
                if (e.key === 'Enter' && e.target === tr) window.location.href = tr.dataset.href;
            });
        });
    }

    function initToolbar(form) {
        if (!form) return;
        form.querySelectorAll('[data-dt-autosubmit]').forEach(el => el.addEventListener('change', () => form.submit()));
        const search = form.querySelector('[data-dt-search]');
        if (!search) return;
        let timer;
        search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => form.submit(), 450); });
        if (search.value) { search.focus(); search.setSelectionRange(search.value.length, search.value.length); }
    }

    function initBulk(opts) {
        const bulkForm = opts.bulkForm ? document.querySelector(opts.bulkForm) : null;
        if (!bulkForm) return null;
        const selectAll = document.getElementById('selectAll');
        const checks = Array.from(document.querySelectorAll('[data-row-check]:not(:disabled)'));
        const countEl = bulkForm.querySelector('#bulkCount');
        const actionInput = opts.actionInput ? document.querySelector(opts.actionInput) : null;
        const actions = opts.actions || {};
        const selected = () => checks.filter(c => c.checked);

        function refresh() {
            const n = selected().length;
            if (countEl) countEl.textContent = n;
            bulkForm.classList.toggle('is-visible', n > 0);
            checks.forEach(c => c.closest('tr').classList.toggle('is-selected', c.checked));
            if (selectAll) {
                selectAll.checked = n > 0 && n === checks.length;
                selectAll.indeterminate = n > 0 && n < checks.length;
                selectAll.disabled = checks.length === 0;
            }
        }

        if (selectAll) selectAll.addEventListener('change', () => { checks.forEach(c => { c.checked = selectAll.checked; }); refresh(); });
        checks.forEach(c => c.addEventListener('change', refresh));
        const clearBtn = bulkForm.querySelector('#bulkClear');
        if (clearBtn) clearBtn.addEventListener('click', () => { checks.forEach(c => { c.checked = false; }); refresh(); });

        // `boxes`: the checkboxes the action applies to (or null for a single-row button).
        function run(action, labels, beforeSubmit, boxes) {
            const cfg = actions[action] || {};
            let skipped = [];
            if (boxes && typeof cfg.eligible === 'function') {
                const ok = boxes.filter(cfg.eligible);
                skipped = boxes.filter(b => !cfg.eligible(b));
                if (!ok.length) {
                    gwConfirm({
                        title: cfg.blockedTitle || 'Nothing to process',
                        message: cfg.blockedMessage || 'None of the selected rows can be processed.',
                        items: skipped.map(b => b.dataset.label + (b.dataset.reason ? ' — ' + b.dataset.reason : '')),
                        confirmText: 'OK',
                        cancelText: 'Close',
                        variant: 'warning',
                        icon: 'bi-lock',
                    });
                    return;
                }
                labels = ok.map(b => b.dataset.label);
                const prevBefore = beforeSubmit;
                beforeSubmit = () => {
                    skipped.forEach(b => { b.checked = false; }); // never post rows that are not eligible
                    if (prevBefore) prevBefore();
                };
            }
            const n = labels.length;
            let note = cfg.note || '';
            if (skipped.length) {
                note = skipped.length + ' selected row' + (skipped.length > 1 ? 's' : '') +
                    ' will be skipped (' + skipped.map(b => b.dataset.label.split(' ')[0]).slice(0, 4).join(', ') +
                    (skipped.length > 4 ? '…' : '') + ').' + (note ? ' ' + note : '');
            }
            gwConfirm({
                title: typeof cfg.title === 'function' ? cfg.title(n) : (cfg.title || 'Are you sure?'),
                message: cfg.message,
                items: labels,
                note: note,
                confirmText: cfg.confirmText,
                variant: cfg.variant,
                icon: cfg.icon,
            }).then(ok => {
                if (!ok) return;
                if (beforeSubmit) beforeSubmit();
                if (actionInput) actionInput.value = action;
                bulkForm.submit();
            });
        }

        bulkForm.querySelectorAll('[data-bulk]').forEach(btn => btn.addEventListener('click', () => {
            const sel = selected();
            if (sel.length) run(btn.dataset.bulk, sel.map(c => c.dataset.label), null, sel);
        }));

        document.querySelectorAll('[data-row-delete]').forEach(btn => btn.addEventListener('click', () => {
            run('delete', [btn.dataset.label], () => {
                checks.forEach(c => { c.checked = false; });
                const hidden = document.createElement('input');
                hidden.type = 'hidden';
                hidden.name = 'selected';
                hidden.value = btn.dataset.rowDelete;
                bulkForm.appendChild(hidden);
            });
        }));

        refresh();

        return {
            selected,
            hasSelection: () => selected().length > 0,
            onChange(fn) { checks.forEach(c => c.addEventListener('change', fn)); if (selectAll) selectAll.addEventListener('change', fn); if (clearBtn) clearBtn.addEventListener('click', fn); },
            toggleRow(tr) {
                const box = tr.querySelector('[data-row-check]');
                if (!box || box.disabled) return;
                box.checked = !box.checked;
                box.dispatchEvent(new Event('change', { bubbles: true }));
            },
        };
    }

    window.GwDatatable = {
        init(opts) {
            opts = opts || {};
            initToolbar(opts.toolbar ? document.querySelector(opts.toolbar) : null);
            const bulk = initBulk(opts);
            initRowClick(document, bulk);
            return bulk;
        },
    };
})();
