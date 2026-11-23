/**
 * gwFileViewer: opens uploaded proofs (receipts, leave attachments) inside the portal
 * in a modal instead of a new browser tab, with a Download button.
 *
 * Every link pointing at /media/ is picked up automatically. Opt out with
 * data-gw-no-preview, or add a `download` attribute to a link to download directly.
 * Optional: data-gw-filename="Bill.pdf" sets the name shown and used for download.
 *
 *   gwFileViewer.open(url, filename)
 */
(function () {
    'use strict';

    const MEDIA_PREFIX = '/media/';
    const IMAGE_RE = /\.(png|jpe?g|gif|webp|bmp|svg|heic|heif)$/i;
    const PDF_RE = /\.pdf$/i;

    let modalEl = null;
    let modal = null;

    function build() {
        if (modalEl) return;
        modalEl = document.createElement('div');
        modalEl.className = 'modal fade';
        modalEl.id = 'gwFileViewerModal';
        modalEl.tabIndex = -1;
        modalEl.setAttribute('aria-hidden', 'true');
        modalEl.innerHTML = `
            <div class="modal-dialog modal-xl modal-dialog-centered modal-dialog-scrollable modal-fullscreen-md-down">
                <div class="modal-content border-0" style="border-radius:16px;overflow:hidden;">
                    <div class="modal-header py-2 px-3">
                        <h6 class="modal-title text-truncate me-3 mb-0" data-gw-fv-title>
                            <i class="bi bi-paperclip me-1"></i><span></span>
                        </h6>
                        <div class="ms-auto d-flex align-items-center gap-2">
                            <a class="btn btn-sm btn-primary" data-gw-fv-download download>
                                <i class="bi bi-download me-1"></i>Download
                            </a>
                            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                        </div>
                    </div>
                    <div class="modal-body p-0 bg-light d-flex align-items-center justify-content-center"
                         style="min-height:70vh;" data-gw-fv-body></div>
                </div>
            </div>`;
        document.body.appendChild(modalEl);
        modal = new bootstrap.Modal(modalEl);
        // Unload the file when closed so PDFs stop rendering / large images free memory.
        modalEl.addEventListener('hidden.bs.modal', () => {
            modalEl.querySelector('[data-gw-fv-body]').innerHTML = '';
        });
    }

    function nameFromUrl(url) {
        try {
            const path = new URL(url, window.location.href).pathname;
            return decodeURIComponent(path.split('/').pop() || 'file');
        } catch (e) {
            return 'file';
        }
    }

    function open(url, filename) {
        if (!window.bootstrap) {
            window.open(url, '_blank', 'noopener');
            return;
        }
        build();
        const name = filename || nameFromUrl(url);
        const path = (function () {
            try { return new URL(url, window.location.href).pathname; } catch (e) { return url; }
        })();
        // Decide by the stored file's extension; fall back to the display name.
        const probe = IMAGE_RE.test(path) || PDF_RE.test(path) ? path : name;

        modalEl.querySelector('[data-gw-fv-title] span').textContent = name;
        const dl = modalEl.querySelector('[data-gw-fv-download]');
        dl.href = url;
        dl.setAttribute('download', name);

        const body = modalEl.querySelector('[data-gw-fv-body]');
        body.innerHTML = '';
        if (IMAGE_RE.test(probe)) {
            const img = document.createElement('img');
            img.src = url;
            img.alt = name;
            img.style.cssText = 'max-width:100%;max-height:80vh;object-fit:contain;display:block;margin:auto;';
            img.onerror = () => showFallback(body, url, name);
            body.appendChild(img);
        } else if (PDF_RE.test(probe)) {
            const frame = document.createElement('iframe');
            frame.src = url;
            frame.title = name;
            frame.style.cssText = 'width:100%;height:80vh;border:0;background:#fff;';
            body.appendChild(frame);
        } else {
            showFallback(body, url, name);
        }
        modal.show();
    }

    function showFallback(body, url, name) {
        body.innerHTML = `
            <div class="text-center text-muted p-5">
                <i class="bi bi-file-earmark fs-1 d-block mb-2"></i>
                <p class="mb-3">A preview isn't available for this file.</p>
                <a class="btn btn-primary" download></a>
            </div>`;
        const a = body.querySelector('a');
        a.href = url;
        a.setAttribute('download', name);
        a.innerHTML = '<i class="bi bi-download me-1"></i>Download';
    }

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

    document.addEventListener('click', (ev) => {
        if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
        const a = ev.target.closest('a[href]');
        if (!a || a.hasAttribute('download') || a.hasAttribute('data-gw-no-preview') || !isMediaLink(a)) return;
        ev.preventDefault();
        const name = a.dataset.gwFilename || nameFromUrl(a.href);
        // Close any open dropdown (e.g. the receipts menu on the approvals table) first.
        const dd = a.closest('.dropdown-menu');
        if (dd && window.bootstrap) {
            const toggle = dd.parentElement.querySelector('[data-bs-toggle="dropdown"]');
            if (toggle) bootstrap.Dropdown.getOrCreateInstance(toggle).hide();
        }
        open(a.href, name);
    });

    window.gwFileViewer = { open };
})();
