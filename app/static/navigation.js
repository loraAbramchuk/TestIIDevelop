(() => {
  const toggle = document.getElementById('menu-toggle');
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  const close = document.getElementById('sidebar-close');
  if (!toggle || !sidebar || !backdrop || !close) return;
  const desktop = window.matchMedia('(min-width: 1000px)');
  let collapsed = false, opened = false;
  function render() {
    const visible = desktop.matches ? !collapsed : opened;
    document.body.classList.toggle('sidebar-collapsed', desktop.matches && collapsed);
    document.body.classList.toggle('sidebar-open', !desktop.matches && opened);
    const modal = !desktop.matches && opened;
    for (const node of document.querySelectorAll('main, .site-search, .header-actions, .brand, .session-notice')) node.inert = modal;
    sidebar.inert = !visible;
    sidebar.setAttribute('aria-hidden', String(!visible));
    toggle.setAttribute('aria-expanded', String(visible));
    toggle.setAttribute('aria-label', visible ? 'Скрыть меню' : 'Открыть меню');
    backdrop.hidden = desktop.matches || !opened;
    if (!desktop.matches && opened) {
      sidebar.setAttribute('role', 'dialog');
      sidebar.setAttribute('aria-modal', 'true');
    } else {
      sidebar.removeAttribute('role');
      sidebar.removeAttribute('aria-modal');
    }
  }
  function dismiss(restoreFocus = true) {
    opened = false;
    render();
    if (restoreFocus) toggle.focus();
  }
  toggle.hidden = false;
  toggle.addEventListener('click', () => {
    if (desktop.matches) collapsed = !collapsed;
    else opened = !opened;
    render();
    if (!desktop.matches && opened) {
      close.focus({preventScroll: true});
      requestAnimationFrame(() => {
        if (opened && !desktop.matches && !sidebar.contains(document.activeElement)) close.focus({preventScroll: true});
      });
    }
  });
  close.addEventListener('click', () => dismiss());
  backdrop.addEventListener('click', () => dismiss());
  sidebar.addEventListener('click', event => {
    if (!desktop.matches && event.target.closest('a')) dismiss(false);
  });
  const account = document.querySelector('.account-menu');
  document.addEventListener('click', event => {
    if (account && !account.contains(event.target)) account.open = false;
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && account?.open) {
      account.open = false;
      account.querySelector('summary').focus();
    }
    if (!desktop.matches && opened) {
      if (event.key === 'Escape') { event.preventDefault(); dismiss(); }
      if (event.key === 'Tab') {
        const items = [...sidebar.querySelectorAll('a[href], button:not([disabled])')].filter(node => node.getClientRects().length && !node.hidden);
        const first = items[0], last = items.at(-1);
        if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !sidebar.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
      }
    }
  });
  desktop.addEventListener('change', () => {
    const hadFocus = sidebar.contains(document.activeElement);
    opened = false;
    render();
    if (hadFocus && (sidebar.inert || !document.activeElement.getClientRects().length)) toggle.focus();
  });
  window.addEventListener('pageshow', () => { opened = false; render(); });
  render();
})();
