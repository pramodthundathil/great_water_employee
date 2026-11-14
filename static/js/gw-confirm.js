/**
 * gwConfirm — themed replacement for window.confirm().
 *
 *   gwConfirm({ title, message, items: [..], note, confirmText, variant: 'danger'|'warning'|'primary', icon })
 *     .then(ok => { if (ok) ... });
 *
 * Forms can also opt in declaratively:
 *   <form data-gw-confirm="Withdraw this claim?" data-gw-confirm-variant="danger"> ...
 */
(function () {
    'use strict';

    const VARIANTS = {
        danger:  { btn: 'btn-gw-danger',  icon: 'bi-trash3' },
        warning: { btn: 'btn-gw-warning', icon: 'bi-exclamation-triangle' },
        primary: { btn: 'btn-gw-primary', icon: 'bi-question-circle' },
    };

    function gwConfirm(opts) {
        opts = opts || {};
        const el = document.getElementById('gwConfirmModal');
        if (!el || !window.bootstrap) return Promise.resolve(window.confirm(opts.message || opts.title || 'Are you sure?'));

        const v = VARIANTS[opts.variant] || VARIANTS.primary;
        const $ = sel => el.querySelector(sel);
        const iconWrap = $('[data-gw-confirm-icon]');
        const okBtn = $('[data-gw-confirm-ok]');
        const list = $('[data-gw-confirm-list]');
        const note = $('[data-gw-confirm-note]');

        el.dataset.variant = opts.variant || 'primary';
        iconWrap.innerHTML = '<i class="bi ' + (opts.icon || v.icon) + '"></i>';
        $('[data-gw-confirm-title]').textContent = opts.title || 'Are you sure?';
        $('[data-gw-confirm-message]').textContent = opts.message || '';

        list.innerHTML = '';
        const items = opts.items || [];
        const max = 6;
        items.slice(0, max).forEach(t => {
            const li = document.createElement('li');
            li.textContent = t;
            list.appendChild(li);
        });
        if (items.length > max) {
            const li = document.createElement('li');
            li.className = 'is-more';
            li.textContent = '+ ' + (items.length - max) + ' more';
            list.appendChild(li);
        }
        list.classList.toggle('d-none', items.length === 0);

        note.textContent = opts.note || '';
        note.classList.toggle('d-none', !opts.note);

        okBtn.className = 'btn ' + v.btn;
        okBtn.textContent = opts.confirmText || 'Confirm';
        $('[data-gw-confirm-cancel]').textContent = opts.cancelText || 'Cancel';

        const modal = bootstrap.Modal.getOrCreateInstance(el);
        return new Promise(resolve => {
            let result = false;
            const onOk = () => { result = true; modal.hide(); };
            const onHidden = () => {
                okBtn.removeEventListener('click', onOk);
                el.removeEventListener('hidden.bs.modal', onHidden);
                resolve(result);
            };
            okBtn.addEventListener('click', onOk);
            el.addEventListener('hidden.bs.modal', onHidden);
            el.addEventListener('shown.bs.modal', () => okBtn.focus(), { once: true });
            modal.show();
        });
    }

    // Declarative forms: <form data-gw-confirm="message">
    document.addEventListener('submit', function (e) {
        const form = e.target;
        if (!form.matches || !form.matches('form[data-gw-confirm]') || form.dataset.gwConfirmed === '1') return;
        e.preventDefault();
        const submitter = e.submitter;
        gwConfirm({
            title: form.dataset.gwConfirmTitle || 'Please confirm',
            message: form.dataset.gwConfirm,
            confirmText: form.dataset.gwConfirmOk,
            variant: form.dataset.gwConfirmVariant,
        }).then(ok => {
            if (!ok) return;
            form.dataset.gwConfirmed = '1';
            if (form.requestSubmit) form.requestSubmit(submitter || undefined); else form.submit();
        });
    }, true);

    window.gwConfirm = gwConfirm;
})();
