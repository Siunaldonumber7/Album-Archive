(() => {
    'use strict';

    const body = document.body;

    if (!body) {
        return;
    }

    function syncCanonicalUrl() {
        const canonicalUrl = (
            body.dataset.canonicalUrl
            || ''
        ).trim();

        if (!canonicalUrl) {
            return;
        }

        const current = new URL(
            window.location.href
        );
        const canonical = new URL(
            canonicalUrl,
            window.location.href
        );

        if (
            current.pathname === canonical.pathname
            && current.search === canonical.search
        ) {
            return;
        }

        window.history.replaceState(
            { albumArchiveCanonical: true },
            '',
            canonical.href
        );
    }

    function buildFormUrl(form) {
        const url = new URL(
            form.action || window.location.href,
            window.location.href
        );
        url.search = '';

        const data = new FormData(form);

        for (const [key, value] of data.entries()) {
            url.searchParams.append(
                key,
                String(value)
            );
        }

        return url;
    }

    function canHandleLink(event, link) {
        if (
            event.defaultPrevented
            || event.button !== 0
            || event.metaKey
            || event.ctrlKey
            || event.shiftKey
            || event.altKey
            || !link
            || !link.href
        ) {
            return false;
        }

        const url = new URL(
            link.href,
            window.location.href
        );

        return url.origin === window.location.origin;
    }

    function createFragmentNavigator({
        replaceSelectors,
        linkSelector,
        formSelector,
        selectSelector,
        cleanVisibleUrl,
        preserveScroll = false,
        requestHeader = 'AlbumArchivePartial'
    }) {
        let activeController = null;

        async function swap(targetUrl, pushHistory = true) {
            const previousScrollY = window.scrollY;

            if (activeController) {
                activeController.abort();
            }

            activeController = new AbortController();

            try {
                const response = await fetch(
                    targetUrl,
                    {
                        signal: activeController.signal,
                        headers: {
                            'X-Requested-With': requestHeader
                        }
                    }
                );

                if (!response.ok) {
                    throw new Error(
                        'Partial request failed: '
                        + response.status
                    );
                }

                const html = await response.text();
                const nextDocument = new DOMParser()
                    .parseFromString(
                        html,
                        'text/html'
                    );

                for (const selector of replaceSelectors) {
                    const current = document.querySelector(
                        selector
                    );
                    const next = nextDocument.querySelector(
                        selector
                    );

                    if (!current || !next) {
                        throw new Error(
                            'Partial target missing: '
                            + selector
                        );
                    }

                    current.replaceWith(next);
                }

                document.title = nextDocument.title;

                if (pushHistory) {
                    const visibleUrl = cleanVisibleUrl
                        ? cleanVisibleUrl(
                            response.url || targetUrl
                        )
                        : new URL(
                            response.url || targetUrl,
                            window.location.href
                        );

                    window.history.pushState(
                        { albumArchivePartial: true },
                        '',
                        visibleUrl.href
                    );
                }

                if (preserveScroll) {
                    window.requestAnimationFrame(() => {
                        window.scrollTo(
                            0,
                            previousScrollY
                        );
                    });
                }
            } catch (error) {
                if (
                    error
                    && error.name === 'AbortError'
                ) {
                    return;
                }

                window.location.assign(targetUrl);
            }
        }

        if (linkSelector) {
            document.addEventListener(
                'click',
                (event) => {
                    const link = event.target.closest(
                        linkSelector
                    );

                    if (!canHandleLink(event, link)) {
                        return;
                    }

                    const url = new URL(
                        link.href,
                        window.location.href
                    );

                    if (
                        url.pathname
                        !== window.location.pathname
                    ) {
                        return;
                    }

                    event.preventDefault();
                    swap(
                        url.href,
                        true
                    );
                }
            );
        }

        if (formSelector) {
            document.addEventListener(
                'submit',
                (event) => {
                    const form = event.target.closest(
                        formSelector
                    );

                    if (!form) {
                        return;
                    }

                    event.preventDefault();

                    swap(
                        buildFormUrl(form).href,
                        true
                    );
                }
            );
        }

        if (selectSelector) {
            document.addEventListener(
                'change',
                (event) => {
                    const select = event.target.closest(
                        selectSelector
                    );

                    if (
                        !select
                        || !select.form
                    ) {
                        return;
                    }

                    select.form.requestSubmit();
                }
            );
        }

        window.addEventListener(
            'popstate',
            () => {
                swap(
                    window.location.href,
                    false
                );
            }
        );

        return {
            swap
        };
    }

    function initIndexFilters() {
        if (body.dataset.page !== 'index') {
            return;
        }

        createFragmentNavigator({
            replaceSelectors: [
                '.side-filter-panel',
                '.top-search',
                '.content'
            ],
            linkSelector: [
                '.side-chip',
                '.chip',
                '.js-filter-reset',
                '.page-link'
            ].join(','),
            formSelector: [
                '.top-search',
                '.side-genre-form',
                '.side-sort-form',
                '.genre-form',
                '.sort-form'
            ].join(','),
            selectSelector: [
                '.side-genre-select',
                '.side-sort-select',
                '.genre-select',
                '.sort-select'
            ].join(','),
            cleanVisibleUrl: (targetUrl) => {
                const url = new URL(
                    targetUrl,
                    window.location.href
                );
                url.searchParams.delete(
                    'reset_filters'
                );
                return url;
            },
            preserveScroll: true,
            requestHeader: 'AlbumArchiveFilter'
        });
    }

    function initArtistDetailFilters() {
        if (
            body.dataset.page
            !== 'artist-detail'
        ) {
            return;
        }

        createFragmentNavigator({
            replaceSelectors: [
                '#artist-results'
            ],
            linkSelector:
                '#artist-results .page-link',
            formSelector:
                '#artist-results .sort-form',
            selectSelector:
                '#artist-results .js-artist-sort',
            preserveScroll: false,
            requestHeader:
                'AlbumArchiveArtistFilter'
        });
    }

    function initArtistProfileImages() {
        if (
            body.dataset.page
            !== 'artist-directory'
        ) {
            return;
        }

        const images = Array.from(
            document.querySelectorAll(
                '.js-artist-profile'
            )
        );

        const loadImage = (img) => {
            if (
                !img
                || img.dataset.loaded === '1'
            ) {
                return;
            }

            img.dataset.loaded = '1';

            const profileSrc =
                img.dataset.profileSrc || '';
            const fallbackSrc =
                img.dataset.fallbackSrc || '';

            let fallbackUsed = false;

            img.addEventListener(
                'error',
                () => {
                    if (
                        !fallbackUsed
                        && fallbackSrc
                    ) {
                        fallbackUsed = true;
                        img.classList.add(
                            'is-fallback'
                        );
                        img.src = fallbackSrc;
                        return;
                    }

                    img.style.display = 'none';
                }
            );

            if (profileSrc) {
                img.src = profileSrc;
            } else if (fallbackSrc) {
                fallbackUsed = true;
                img.classList.add(
                    'is-fallback'
                );
                img.src = fallbackSrc;
            } else {
                img.style.display = 'none';
            }
        };

        if ('IntersectionObserver' in window) {
            const observer = new IntersectionObserver(
                (entries) => {
                    for (const entry of entries) {
                        if (!entry.isIntersecting) {
                            continue;
                        }

                        observer.unobserve(
                            entry.target
                        );
                        loadImage(
                            entry.target
                        );
                    }
                },
                {
                    rootMargin: '320px 0px'
                }
            );

            images.forEach(
                (img) => observer.observe(img)
            );
            return;
        }

        images.forEach(loadImage);
    }

    syncCanonicalUrl();
    initIndexFilters();
    initArtistDetailFilters();
    initArtistProfileImages();
})();
