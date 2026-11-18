const channelProfile = document.getElementById('channel-profile');
if (channelProfile) {
  const base = `/api/channels/${channelProfile.dataset.channelId}`;
  const subscribe = document.getElementById('subscribe');
  if (subscribe) subscribe.addEventListener('click', async () => {
    if (subscribe.disabled) return;
    subscribe.disabled = true;
    const error = document.getElementById('subscription-error');
    error.textContent = '';
    try {
      const method = subscribe.dataset.subscribed === 'true' ? 'DELETE' : 'PUT';
      const state = await apiRequest(base + '/subscription', method);
      subscribe.dataset.subscribed = String(state.is_subscribed);
      subscribe.setAttribute('aria-pressed', String(state.is_subscribed));
      subscribe.textContent = state.is_subscribed ? 'Отписаться' : 'Подписаться';
      document.getElementById('subscriber-count').textContent = state.subscriber_count;
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? 'Ошибка соединения. Повторите попытку.' : failure.message;
    } finally { subscribe.disabled = false; }
  });
  const form = document.getElementById('channel-edit');
  if (form) form.addEventListener('submit', async event => {
    event.preventDefault();
    const controls = [...form.querySelectorAll('input, textarea, button')];
    const error = form.querySelector('[data-error]'), success = form.querySelector('[data-success]');
    error.textContent = ''; success.textContent = '';
    const values = Object.fromEntries(new FormData(form));
    for (const control of controls) control.disabled = true;
    try {
      const channel = await apiRequest(base, 'PATCH', values);
      for (const label of document.querySelectorAll('[data-channel-label]')) {
        if (label.dataset.channelId === channel.id) label.textContent = channel.name;
      }
      document.getElementById('channel-description').textContent = channel.description;
      form.elements.name.value = channel.name;
      form.elements.description.value = channel.description;
      document.title = `${channel.name} — ${channelProfile.dataset.appName}`;
      success.textContent = 'Изменения сохранены.';
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? 'Ошибка соединения. Повторите попытку.' : failure.message;
    } finally { for (const control of controls) control.disabled = false; }
  });
}
