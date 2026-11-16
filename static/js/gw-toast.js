/**
 * gwToast — app-wide toast notifications with auto-hide.
 *
 *   gwToast('Saved.', 'success');            // success | danger | error | warning | info
 *   gwToast('Failed.', 'danger', { timeout: 8000 });  // timeout 0 = stay until closed
 *
 * Server-rendered toasts: any element with [data-gw-toast] (Django messages, form
 * errors; optional data-type / data-timeout) is moved into the stack on page load.
 * Hovering or focusing a toast pauses its timer.
 */
(function () {
    'use strict';

    const TYPES = {
        success: { icon: 'bi-check-circle-fill',        title: 'Success', timeout: 4000 },
        danger:  { icon: 'bi-x-octagon-fill',           title: 'Error',   timeout: 7000 },
        warning: { icon: 'bi-exclamation-triangle-fill', title: 'Warning', timeout: 6000 },
        info:    { icon: 'bi-info-circle-fill',         title: 'Info',    timeout: 5000 },
    };

    function normalise(type) {
        type = String(type || 'info').toLowerCase();
        if (type === 'error') return 'danger';
        return TYPES[type] ? type : 'info';
    }

    function stack() {
        let el = document.getElementById('gwToastStack');
        if (!el) {
            el = document.createElement('div');
            el.id = 'gwToastStack';
            el.className = 'gw-toast-stack';
            el.setAttribute('aria-live', 'polite');
            document.body.appendChild(el);
        }
        return el;
    }

    function dismiss(toast) {
        if (toast.dataset.closing) return;
        toast.dataset.closing = '1';
        clearTimeout(toast._gwTimer);
        toast.classList.remove('is-visible');
        toast.classList.add('is-leaving');
        setTimeout(() => toast.remove(), 260);
    }

    function startTimer(toast, ms) {
        const bar = toast.querySelector('.gw-toast-progress > span');
        let remaining = ms, started;
        const run = () => {
            started = Date.now();
            toast._gwTimer = setTimeout(() => dismiss(toast), remaining);
            if (bar) {
                bar.style.transition = 'transform ' + remaining + 'ms linear';
                bar.style.transform = 'scaleX(0)';
            }
        };
        const pause = () => {
            clearTimeout(toast._gwTimer);
            remaining -= Date.now() - started;
            if (bar) {
                const scale = Math.max(remaining / ms, 0);
                bar.style.transition = 'none';
                bar.style.transform = 'scaleX(' + scale + ')';
            }
        };
        toast.addEventListener('mouseenter', pause);
        toast.addEventListener('mouseleave', run);
        toast.addEventListener('focusin', pause);
        toast.addEventListener('focusout', run);
        requestAnimationFrame(() => requestAnimationFrame(run));
    }

    /** Turn a bare element (text content = message) into a full toast. */
    function build(toast, message, type, opts) {
        type = normalise(type);
        const cfg = TYPES[type];
        opts = opts || {};
        const timeout = opts.timeout === undefined ? cfg.timeout : opts.timeout;

        toast.className = 'gw-toast gw-toast--' + type;
        toast.dataset.type = type;
        if (!toast.getAttribute('role')) toast.setAttribute('role', type === 'danger' ? 'alert' : 'status');
        toast.innerHTML = '';

        const icon = document.createElement('i');
        icon.className = 'bi ' + cfg.icon + ' gw-toast-icon';
        icon.setAttribute('aria-hidden', 'true');

        const body = document.createElement('div');
        body.className = 'gw-toast-body';
        const title = document.createElement('div');
        title.className = 'gw-toast-title';
        title.textContent = opts.title || cfg.title;
        const text = document.createElement('div');
        text.className = 'gw-toast-text';
        text.textContent = message;
        body.append(title, text);

        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'gw-toast-close';
        close.setAttribute('aria-label', 'Dismiss notification');
        close.innerHTML = '<i class="bi bi-x-lg"></i>';
        close.addEventListener('click', () => dismiss(toast));

        toast.append(icon, body, close);

        if (timeout > 0) {
            const prog = document.createElement('div');
            prog.className = 'gw-toast-progress';
            prog.innerHTML = '<span></span>';
            toast.appendChild(prog);
        }

        requestAnimationFrame(() => toast.classList.add('is-visible'));
        if (timeout > 0) startTimer(toast, timeout);
        return toast;
    }

    function gwToast(message, type, opts) {
        if (!message) return null;
        const toast = document.createElement('div');
        stack().appendChild(toast);
        const s = stack();
        // Keep the stack short: drop the oldest beyond 5.
        while (s.children.length > 5) dismiss(s.firstElementChild);
        return build(toast, String(message), type, opts);
    }

    function upgradeServerToasts() {
        const s = stack();
        document.querySelectorAll('[data-gw-toast]').forEach((el, i) => {
            const msg = el.textContent.replace(/\s+/g, ' ').trim();
            el.removeAttribute('data-gw-toast');
            el.hidden = false;
            if (!msg) { el.remove(); return; }
            if (el.parentElement !== s) s.appendChild(el);
            // Stagger slightly so several messages don't appear as one block.
            el.style.transitionDelay = (i * 80) + 'ms';
            build(el, msg, el.dataset.type, { timeout: el.dataset.timeout ? Number(el.dataset.timeout) : undefined });
        });
    }

    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') {
            const last = document.querySelector('#gwToastStack .gw-toast:last-child');
            if (last) dismiss(last);
        }
    });

    window.gwToast = gwToast;
    ['success', 'error', 'warning', 'info'].forEach(t => {
        gwToast[t] = (msg, opts) => gwToast(msg, t, opts);
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', upgradeServerToasts);
    } else {
        upgradeServerToasts();
    }
})();
