/**
 * gwFileViewer: full-screen viewer for uploaded proofs (receipts, leave attachments).
 *
 * Every link pointing at /media/ opens here instead of a new tab. Links that share the same
 * data-gw-gallery="…" value form a gallery: previous / next, thumbnails, "2 / 5" counter.
 *
 *   Images: zoom (buttons, mouse wheel, double-click, pinch), drag to pan, rotate, fit.
 *   PDFs:   shown full height with the browser's PDF viewer.
 *   Keys:   ← / → previous / next · + / − zoom · 0 fit · R rotate · F full screen · Esc close.
 *
 * Opt out with data-gw-no-preview; a link with a `download` attribute downloads directly.
 * data-gw-filename="Bill.pdf" sets the name shown and used for download.
 *
 *   gwFileViewer.open(url, filename)
 *   gwFileViewer.openGallery([{url, name}, …], startIndex)
 */
(function () {
    'use strict';

    const MEDIA_PREFIX = '/media/';
    const IMAGE_RE = /\.(png|jpe?g|gif|webp|bmp|svg|heic|heif|avif)$/i;
    const HEIC_RE = /\.(heic|heif)$/i;
    const PDF_RE = /\.pdf$/i;
    const MIN_ZOOM = 0.25, MAX_ZOOM = 8;

    let root = null, els = {};
    let items = [], index = 0;
    let zoom = 1, rot = 0, tx = 0, ty = 0;
    let lastFocus = null;

    const CSS = `
    .gwfv{position:fixed;inset:0;z-index:2000;display:none;flex-direction:column;background:rgba(12,16,22,.96);color:#e8edf3;font-size:14px}
    .gwfv.is-open{display:flex}
    .gwfv-bar{display:flex;align-items:center;gap:6px;padding:8px 12px;background:rgba(0,0,0,.35);flex-wrap:wrap}
    .gwfv-title{flex:1 1 200px;min-width:0;display:flex;align-items:center;gap:8px}
    .gwfv-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .gwfv-count{flex-shrink:0;font-size:12px;padding:2px 8px;border-radius:999px;background:rgba(255,255,255,.12)}
    .gwfv-tools{display:flex;align-items:center;gap:4px;flex-wrap:wrap}
    .gwfv-btn{border:0;background:rgba(255,255,255,.08);color:#e8edf3;height:34px;min-width:34px;padding:0 10px;border-radius:8px;display:inline-flex;align-items:center;justify-content:center;gap:6px;text-decoration:none;font-size:14px;cursor:pointer}
    .gwfv-btn:hover,.gwfv-btn:focus-visible{background:rgba(255,255,255,.2);color:#fff;outline:none}
    .gwfv-btn[disabled]{opacity:.35;pointer-events:none}
    .gwfv-btn.primary{background:#156B9E}.gwfv-btn.primary:hover{background:#1a80bd}
    .gwfv-zoomval{min-width:52px;text-align:center;font-variant-numeric:tabular-nums;font-size:12px;opacity:.85}
    .gwfv-sep{width:1px;height:22px;background:rgba(255,255,255,.15);margin:0 4px}
    .gwfv-stage{position:relative;flex:1;min-height:0;overflow:hidden;display:flex;align-items:center;justify-content:center;touch-action:none}
    .gwfv-stage img{max-width:100%;max-height:100%;object-fit:contain;transform-origin:center center;user-select:none;-webkit-user-drag:none;transition:transform .12s ease-out;cursor:zoom-in}
    .gwfv-stage.is-zoomed img{cursor:grab}.gwfv-stage.is-dragging img{cursor:grabbing;transition:none}
    .gwfv-stage iframe{width:100%;height:100%;border:0;background:#fff}
    .gwfv-nav{position:absolute;top:50%;transform:translateY(-50%);width:48px;height:48px;border-radius:50%;border:0;background:rgba(0,0,0,.45);color:#fff;font-size:22px;display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:2}
    .gwfv-nav:hover{background:rgba(0,0,0,.7)}.gwfv-prev{left:14px}.gwfv-next{right:14px}
    .gwfv-msg{text-align:center;max-width:420px;padding:24px}.gwfv-msg i{font-size:48px;display:block;margin-bottom:10px;opacity:.7}
    .gwfv-spin{position:absolute;width:38px;height:38px;border:3px solid rgba(255,255,255,.2);border-top-color:#fff;border-radius:50%;animation:gwfvspin .8s linear infinite}
    @keyframes gwfvspin{to{transform:rotate(360deg)}}
    .gwfv-strip{display:flex;gap:8px;padding:10px 12px;overflow-x:auto;background:rgba(0,0,0,.35);justify-content:safe center}
    .gwfv-thumb{flex:0 0 auto;width:64px;height:64px;border-radius:8px;border:2px solid transparent;background:rgba(255,255,255,.08);overflow:hidden;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;color:#e8edf3}
    .gwfv-thumb img{width:100%;height:100%;object-fit:cover}.gwfv-thumb i{font-size:26px}
    .gwfv-thumb.is-active{border-color:#4fb3f0}.gwfv-thumb:hover{border-color:rgba(255,255,255,.5)}
    .gwfv-img-only{display:contents}.gwfv.is-pdf .gwfv-img-only{display:none}
    @media (max-width:640px){.gwfv-hide-sm{display:none!important}.gwfv-nav{width:40px;height:40px}.gwfv-thumb{width:52px;height:52px}}
    body.gwfv-lock{overflow:hidden}`;

    function build() {
        if (root) return;
        const style = document.createElement('style');
        style.textContent = CSS;
        document.head.appendChild(style);

        root = document.createElement('div');
        root.className = 'gwfv';
        root.setAttribute('role', 'dialog');
        root.setAttribute('aria-modal', 'true');
        root.setAttribute('aria-label', 'File viewer');
        root.innerHTML = `
            <div class="gwfv-bar">
                <div class="gwfv-title">
                    <i class="bi bi-paperclip"></i>
                    <span class="gwfv-name" data-el="name"></span>
                    <span class="gwfv-count" data-el="count"></span>
                </div>
                <div class="gwfv-tools">
                    <span class="gwfv-img-only">
                        <button type="button" class="gwfv-btn" data-act="out" title="Zoom out (−)" aria-label="Zoom out"><i class="bi bi-zoom-out"></i></button>
                        <span class="gwfv-zoomval gwfv-hide-sm" data-el="zoom">100%</span>
                        <button type="button" class="gwfv-btn" data-act="in" title="Zoom in (+)" aria-label="Zoom in"><i class="bi bi-zoom-in"></i></button>
                        <button type="button" class="gwfv-btn gwfv-hide-sm" data-act="fit" title="Fit to screen (0)" aria-label="Fit to screen"><i class="bi bi-aspect-ratio"></i></button>
                        <button type="button" class="gwfv-btn" data-act="rotate" title="Rotate (R)" aria-label="Rotate"><i class="bi bi-arrow-clockwise"></i></button>
                        <span class="gwfv-sep gwfv-hide-sm"></span>
                    </span>
                    <button type="button" class="gwfv-btn gwfv-hide-sm" data-act="fullscreen" title="Full screen (F)" aria-label="Full screen"><i class="bi bi-arrows-fullscreen"></i></button>
                    <a class="gwfv-btn gwfv-hide-sm" data-el="newtab" target="_blank" rel="noopener" data-gw-no-preview title="Open in new tab" aria-label="Open in new tab"><i class="bi bi-box-arrow-up-right"></i></a>
                    <a class="gwfv-btn primary" data-el="download" download data-gw-no-preview title="Download"><i class="bi bi-download"></i><span class="gwfv-hide-sm">Download</span></a>
                    <button type="button" class="gwfv-btn" data-act="close" title="Close (Esc)" aria-label="Close"><i class="bi bi-x-lg"></i></button>
                </div>
            </div>
            <div class="gwfv-stage" data-el="stage">
                <button type="button" class="gwfv-nav gwfv-prev" data-act="prev" aria-label="Previous file"><i class="bi bi-chevron-left"></i></button>
                <div data-el="content" style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;"></div>
                <button type="button" class="gwfv-nav gwfv-next" data-act="next" aria-label="Next file"><i class="bi bi-chevron-right"></i></button>
            </div>
            <div class="gwfv-strip" data-el="strip"></div>`;
        document.body.appendChild(root);
        root.querySelectorAll('[data-el]').forEach(e => { els[e.dataset.el] = e; });

        root.addEventListener('click', ev => {
            const btn = ev.target.closest('[data-act]');
            if (btn) { act(btn.dataset.act); return; }
            // Click on the dark background (outside the file) closes.
            if (ev.target === els.stage || ev.target === els.content) close();
        });
        document.addEventListener('keydown', onKey, true);
        bindGestures();
        document.addEventListener('fullscreenchange', () => {
            const i = root.querySelector('[data-act=fullscreen] i');
            if (i) i.className = document.fullscreenElement ? 'bi bi-fullscreen-exit' : 'bi bi-arrows-fullscreen';
        });
    }

    // ---------------------------------------------------------------- helpers
    function pathOf(url) {
        try { return new URL(url, window.location.href).pathname; } catch (e) { return url; }
    }
    function nameFromUrl(url) {
        return decodeURIComponent(pathOf(url).split('/').pop() || 'file');
    }
    function kind(item) {
        const p = pathOf(item.url);
        const probe = IMAGE_RE.test(p) || PDF_RE.test(p) ? p : item.name;
        if (PDF_RE.test(probe)) return 'pdf';
        if (IMAGE_RE.test(probe)) return HEIC_RE.test(probe) ? 'heic' : 'image';
        return 'other';
    }
    function img() { return els.content.querySelector('img'); }

    function applyTransform() {
        const im = img();
        if (!im) return;
        im.style.transform = `translate(${tx}px, ${ty}px) scale(${zoom}) rotate(${rot}deg)`;
        els.zoom.textContent = Math.round(zoom * 100) + '%';
        els.stage.classList.toggle('is-zoomed', zoom > 1.01);
    }
    function setZoom(z, cx, cy) {
        const nz = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
        if (cx !== undefined) {
            // Keep the point under the cursor in place while zooming.
            const r = els.stage.getBoundingClientRect();
            const px = cx - r.left - r.width / 2, py = cy - r.top - r.height / 2;
            tx = px - (px - tx) * (nz / zoom);
            ty = py - (py - ty) * (nz / zoom);
        }
        zoom = nz;
        if (zoom <= 1) { tx = 0; ty = 0; }
        applyTransform();
    }
    function resetView() { zoom = 1; rot = 0; tx = 0; ty = 0; applyTransform(); }

    // ---------------------------------------------------------------- render
    function show(i) {
        index = (i + items.length) % items.length;
        const item = items[index];
        const k = kind(item);
        resetView();
        root.classList.toggle('is-pdf', k !== 'image' && k !== 'heic');
        els.name.textContent = item.name;
        els.name.title = item.name;
        els.count.textContent = items.length > 1 ? `${index + 1} / ${items.length}` : '';
        els.count.style.display = items.length > 1 ? '' : 'none';
        els.download.href = item.url;
        els.download.setAttribute('download', item.name);
        els.newtab.href = item.url;
        root.querySelectorAll('.gwfv-nav').forEach(b => { b.style.display = items.length > 1 ? '' : 'none'; });
        els.strip.querySelectorAll('.gwfv-thumb').forEach((t, n) => {
            t.classList.toggle('is-active', n === index);
            if (n === index) t.scrollIntoView({ block: 'nearest', inline: 'center' });
        });

        const c = els.content;
        c.innerHTML = '<div class="gwfv-spin" aria-hidden="true"></div>';
        if (k === 'image' || k === 'heic') {
            const im = new Image();
            im.alt = item.name;
            im.draggable = false;
            im.onload = () => { c.innerHTML = ''; c.appendChild(im); applyTransform(); };
            im.onerror = () => fallback(item, k === 'heic'
                ? 'This browser can\'t display HEIC photos (Safari can). Download it to view.'
                : 'This image could not be loaded.');
            im.src = item.url;
        } else if (k === 'pdf') {
            const f = document.createElement('iframe');
            f.src = item.url;
            f.title = item.name;
            c.innerHTML = '';
            c.appendChild(f);
        } else {
            fallback(item, 'A preview isn\'t available for this file type.');
        }
        // Warm the neighbours so next / previous feel instant.
        [index + 1, index - 1].forEach(n => {
            const it = items[(n + items.length) % items.length];
            if (it && kind(it) === 'image') { const p = new Image(); p.src = it.url; }
        });
    }

    function fallback(item, msg) {
        els.content.innerHTML = `
            <div class="gwfv-msg">
                <i class="bi bi-file-earmark"></i>
                <p class="mb-3"></p>
                <a class="gwfv-btn primary" download data-gw-no-preview><i class="bi bi-download"></i>Download</a>
            </div>`;
        els.content.querySelector('p').textContent = msg;
        const a = els.content.querySelector('a');
        a.href = item.url;
        a.setAttribute('download', item.name);
    }

    function buildStrip() {
        els.strip.innerHTML = '';
        els.strip.style.display = items.length > 1 ? '' : 'none';
        items.forEach((it, n) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'gwfv-thumb';
            b.title = it.name;
            b.setAttribute('aria-label', `Show ${it.name}`);
            const k = kind(it);
            if (k === 'image') {
                const im = document.createElement('img');
                im.src = it.url; im.alt = ''; im.loading = 'lazy';
                im.onerror = () => { b.innerHTML = '<i class="bi bi-file-earmark-image"></i>'; };
                b.appendChild(im);
            } else {
                b.innerHTML = k === 'pdf' ? '<i class="bi bi-file-earmark-pdf-fill" style="color:#ff6b6b"></i>' : '<i class="bi bi-file-earmark-image"></i>';
            }
            b.addEventListener('click', () => show(n));
            els.strip.appendChild(b);
        });
    }

    // ---------------------------------------------------------------- open / close
    function openGallery(list, start) {
        if (!list || !list.length) return;
        build();
        items = list.map(it => ({ url: it.url, name: it.name || nameFromUrl(it.url) }));
        lastFocus = document.activeElement;
        buildStrip();
        root.classList.add('is-open');
        document.body.classList.add('gwfv-lock');
        show(start || 0);
        root.querySelector('[data-act=close]').focus();
    }

    function open(url, filename) {
        openGallery([{ url, name: filename }], 0);
    }

    function close() {
        if (!root || !root.classList.contains('is-open')) return;
        if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
        root.classList.remove('is-open');
        document.body.classList.remove('gwfv-lock');
        els.content.innerHTML = ''; // stop PDF rendering / free memory
        if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    function act(a) {
        switch (a) {
            case 'close': close(); break;
            case 'prev': if (items.length > 1) show(index - 1); break;
            case 'next': if (items.length > 1) show(index + 1); break;
            case 'in': setZoom(zoom * 1.25); break;
            case 'out': setZoom(zoom / 1.25); break;
            case 'fit': resetView(); break;
            case 'rotate': rot = (rot + 90) % 360; applyTransform(); break;
            case 'fullscreen':
                if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
                else if (root.requestFullscreen) root.requestFullscreen().catch(() => {});
                break;
        }
    }

    function onKey(e) {
        if (!root || !root.classList.contains('is-open')) return;
        const k = e.key;
        const handled = {
            Escape: 'close', ArrowLeft: 'prev', ArrowRight: 'next', '+': 'in', '=': 'in', '-': 'out',
            '0': 'fit', r: 'rotate', R: 'rotate', f: 'fullscreen', F: 'fullscreen',
        }[k];
        if (handled) {
            e.preventDefault();
            e.stopPropagation();
            if (handled === 'close' && document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
            act(handled);
            return;
        }
        if (k === 'Tab') { // keep focus inside the viewer
            const f = Array.from(root.querySelectorAll('button, a[href]')).filter(x => x.offsetParent !== null);
            if (!f.length) return;
            const first = f[0], last = f[f.length - 1];
            if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
            else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
    }

    // ---------------------------------------------------------------- mouse & touch
    function bindGestures() {
        const st = els.stage;
        st.addEventListener('wheel', e => {
            if (!img()) return;
            e.preventDefault();
            setZoom(zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX, e.clientY);
        }, { passive: false });

        st.addEventListener('dblclick', e => {
            if (!img() || !e.target.closest('img')) return;
            if (zoom > 1.01) resetView(); else setZoom(2.5, e.clientX, e.clientY);
        });

        let drag = null;
        const pointers = new Map();
        let pinch = null, swipeX = null;

        st.addEventListener('pointerdown', e => {
            if (!img() || e.target.closest('button')) return;
            pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
            st.setPointerCapture(e.pointerId);
            if (pointers.size === 2) {
                const [a, b] = Array.from(pointers.values());
                pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), z: zoom };
                drag = null;
            } else if (zoom > 1.01) {
                drag = { x: e.clientX - tx, y: e.clientY - ty };
                st.classList.add('is-dragging');
            } else {
                swipeX = e.clientX;
            }
        });
        st.addEventListener('pointermove', e => {
            if (!pointers.has(e.pointerId)) return;
            pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
            if (pinch && pointers.size === 2) {
                const [a, b] = Array.from(pointers.values());
                setZoom(pinch.z * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d, (a.x + b.x) / 2, (a.y + b.y) / 2);
            } else if (drag) {
                tx = e.clientX - drag.x;
                ty = e.clientY - drag.y;
                applyTransform();
            }
        });
        const end = e => {
            if (!pointers.has(e.pointerId)) return;
            pointers.delete(e.pointerId);
            if (pointers.size < 2) pinch = null;
            if (swipeX !== null && pointers.size === 0) {
                const dx = e.clientX - swipeX;
                if (Math.abs(dx) > 60 && items.length > 1) show(index + (dx < 0 ? 1 : -1));
                swipeX = null;
            }
            if (pointers.size === 0) { drag = null; st.classList.remove('is-dragging'); }
        };
        st.addEventListener('pointerup', end);
        st.addEventListener('pointercancel', end);
    }

    // ---------------------------------------------------------------- auto-wire /media/ links
    function isMediaLink(a) {
        const href = a.getAttribute('href');
        if (!href) return false;
        try {
            const u = new URL(href, window.location.href);
            return u.origin === window.location.origin && u.pathname.startsWith(MEDIA_PREFIX);
        } catch (e) {
            return false;
        }
    }

    function galleryFor(a) {
        const group = a.dataset.gwGallery;
        if (!group) return { list: [{ url: a.href, name: a.dataset.gwFilename || nameFromUrl(a.href) }], start: 0 };
        const seen = new Set(), list = [];
        let start = 0;
        document.querySelectorAll('a[href][data-gw-gallery]').forEach(x => {
            if (x.dataset.gwGallery !== group || x.hasAttribute('download') || !isMediaLink(x)) return;
            if (seen.has(x.href)) { if (x === a) start = list.findIndex(i => i.url === x.href); return; }
            seen.add(x.href);
            if (x === a) start = list.length;
            list.push({ url: x.href, name: x.dataset.gwFilename || nameFromUrl(x.href) });
        });
        return { list, start };
    }

    document.addEventListener('click', ev => {
        if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
        const a = ev.target.closest('a[href]');
        if (!a || a.hasAttribute('download') || a.hasAttribute('data-gw-no-preview') || !isMediaLink(a)) return;
        if (root && root.contains(a)) return;
        ev.preventDefault();
        // Close any open dropdown (e.g. the receipts menu on the approvals table) first.
        const dd = a.closest('.dropdown-menu');
        if (dd && window.bootstrap) {
            const toggle = dd.parentElement.querySelector('[data-bs-toggle="dropdown"]');
            if (toggle) bootstrap.Dropdown.getOrCreateInstance(toggle).hide();
        }
        const g = galleryFor(a);
        openGallery(g.list, g.start);
    });

    window.gwFileViewer = { open, openGallery, close };
})();
