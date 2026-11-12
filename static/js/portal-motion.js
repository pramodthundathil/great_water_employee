/**
 * Great Waters - Portal Motion & Micro-Interactions Framework
 * Built following HyperFrames Animation & Antigravity Spatial Design principles
 */

document.addEventListener('DOMContentLoaded', () => {
    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // 1. Staggered Entrance Animations using GSAP (respects prefers-reduced-motion)
    if (typeof gsap !== 'undefined' && !prefersReducedMotion) {
        // Stagger Table Rows
        const rows = document.querySelectorAll('.data-table-row');
        if (rows.length > 0) {
            gsap.from(rows, {
                y: 18,
                opacity: 0,
                duration: 0.42,
                stagger: 0.035,
                ease: 'power2.out',
                clearProps: 'transform,opacity'
            });
        }

        // Stagger KPI Cards if present
        const kpis = document.querySelectorAll('.kpi-card');
        if (kpis.length > 0) {
            gsap.from(kpis, {
                y: 20,
                opacity: 0,
                duration: 0.45,
                stagger: 0.06,
                ease: 'back.out(1.4)',
                clearProps: 'transform,opacity'
            });
        }

        // Pop in Filter Chips
        const chips = document.querySelectorAll('.filter-chip');
        if (chips.length > 0) {
            gsap.from(chips, {
                scale: 0.8,
                opacity: 0,
                duration: 0.3,
                stagger: 0.04,
                ease: 'back.out(1.6)',
                clearProps: 'transform,opacity'
            });
        }
    }

    // 2. Interactive Bookmark / Pin toggles
    document.querySelectorAll('.btn-bookmark').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            btn.classList.toggle('bookmarked');
            const icon = btn.querySelector('i');
            if (btn.classList.contains('bookmarked')) {
                icon.className = 'bi bi-bookmark-fill text-primary';
                if (typeof gsap !== 'undefined') {
                    gsap.fromTo(btn, { scale: 0.7 }, { scale: 1.25, duration: 0.15, yoyo: true, repeat: 1, ease: 'power2.out' });
                }
            } else {
                icon.className = 'bi bi-bookmark';
                if (typeof gsap !== 'undefined') {
                    gsap.fromTo(btn, { scale: 0.8 }, { scale: 1, duration: 0.2, ease: 'power2.out' });
                }
            }
        });
    });

    // 3. Instant Live Client-side Table Search Filter
    const searchInput = document.querySelector('.panel-search-input');
    const tableRows = document.querySelectorAll('.data-panel-table tbody tr.data-table-row');
    const counterBadge = document.querySelector('.panel-count-badge');

    if (searchInput && tableRows.length > 0) {
        searchInput.addEventListener('input', (e) => {
            const query = e.target.value.toLowerCase().trim();
            let visibleCount = 0;

            tableRows.forEach(row => {
                const text = row.innerText.toLowerCase();
                if (text.includes(query)) {
                    row.style.display = '';
                    visibleCount++;
                } else {
                    row.style.display = 'none';
                }
            });

            if (counterBadge) {
                counterBadge.textContent = visibleCount;
                if (typeof gsap !== 'undefined') {
                    gsap.fromTo(counterBadge, { scale: 1.2 }, { scale: 1, duration: 0.2, ease: 'back.out(2)' });
                }
            }
        });
    }

    // 4. Quick Message Modal trigger
    document.querySelectorAll('.btn-direct-chat').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const recipientName = btn.getAttribute('data-name') || 'Colleague';
            const recipientEmail = btn.getAttribute('data-email') || '';
            const modalEl = document.getElementById('quickMessageModal');
            if (modalEl) {
                document.getElementById('quickMsgRecipientName').textContent = recipientName;
                document.getElementById('quickMsgRecipientEmail').textContent = recipientEmail;
                const modal = new bootstrap.Modal(modalEl);
                modal.show();
            } else {
                window.location.href = `mailto:${recipientEmail}?subject=Great Waters Internal Communication`;
            }
        });
    });

    // 5. Accessible responsive sidebar drawer
    const sidebarToggle = document.getElementById('sidebarToggle');
    const sidebarClose = document.getElementById('sidebarClose');
    const sidebarBackdrop = document.getElementById('sidebarBackdrop');
    const appSidebar = document.getElementById('appSidebar');
    const desktopMq = window.matchMedia('(min-width: 992px)');

    if (sidebarToggle && appSidebar) {
        const setSidebarState = (isOpen, { restoreFocus = false } = {}) => {
            const isDesktop = desktopMq.matches;
            const drawerVisible = !isDesktop && isOpen;

            appSidebar.classList.toggle('show', drawerVisible);
            if (sidebarBackdrop) {
                sidebarBackdrop.classList.toggle('show', drawerVisible);
            }
            document.body.classList.toggle('sidebar-open', drawerVisible);
            sidebarToggle.setAttribute('aria-expanded', String(drawerVisible));

            if (isDesktop) {
                appSidebar.removeAttribute('inert');
            } else if (drawerVisible) {
                appSidebar.removeAttribute('inert');
                if (sidebarClose) {
                    sidebarClose.focus({ preventScroll: true });
                }
            } else {
                appSidebar.setAttribute('inert', '');
                if (restoreFocus) {
                    sidebarToggle.focus({ preventScroll: true });
                }
            }
        };

        setSidebarState(false);

        sidebarToggle.addEventListener('click', () => {
            const nextState = !appSidebar.classList.contains('show');
            setSidebarState(nextState);
        });

        if (sidebarClose) {
            sidebarClose.addEventListener('click', () => setSidebarState(false, { restoreFocus: true }));
        }

        if (sidebarBackdrop) {
            sidebarBackdrop.addEventListener('click', () => setSidebarState(false, { restoreFocus: true }));
        }

        appSidebar.querySelectorAll('.nav-link-custom').forEach(link => {
            link.addEventListener('click', () => {
                if (!desktopMq.matches) {
                    setSidebarState(false);
                }
            });
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && appSidebar.classList.contains('show')) {
                setSidebarState(false, { restoreFocus: true });
            }
        });

        desktopMq.addEventListener('change', () => setSidebarState(false));
    }
});
