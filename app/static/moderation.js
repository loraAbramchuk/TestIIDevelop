(() => {
  const form = document.getElementById('video-moderation-form') || document.getElementById('report-decision-form');
  if (!form) return;
  const videoForm = form.id === 'video-moderation-form', card = document.getElementById('moderation-video');
  const videoId = card.dataset.videoId, reportId = form.dataset.reportId;
  const error = document.getElementById('moderation-error'), success = document.getElementById('moderation-success');
  const refresh = document.getElementById('moderation-refresh'), refreshStatus = document.getElementById('moderation-refresh-status');
  const profile = videoForm ? `/api/moderation/videos/${videoId}` : `/api/moderation/reports/${reportId}`;
  const validate = data => videoForm ? ModerationData.video(data, videoId) : ModerationData.report(data, reportId) && data.video_id === videoId;
  let busy = false, refreshing = false, forbidden = false, needsRefresh = false, closed = form.hidden;
  function controls() {
    const disabled = busy || refreshing || forbidden || closed || needsRefresh;
    for (const control of form.elements) control.disabled = disabled;
    if (videoForm) for (const button of form.querySelectorAll('button[value]')) {
      button.disabled = disabled || form.dataset.deleted === 'true' || button.value === form.dataset.state;
    }
    refresh.disabled = busy || refreshing;
  }
  function renderVideo(video) {
    card.hidden = false;
    if (videoForm) { form.dataset.state = video.moderation_status; form.dataset.deleted = String(video.deleted); }
    document.getElementById('moderation-state').textContent = video.moderation_status;
    document.getElementById('moderation-privacy').textContent = video.visibility;
    document.getElementById('moderation-processing').textContent = video.processing_status;
    document.getElementById('moderation-video-title').textContent = video.title || 'Название недоступно по текущим правам просмотра';
    document.getElementById('moderation-current-reason').textContent = video.moderation_reason || '';
    document.getElementById('moderation-deleted').hidden = !video.deleted;
    const watch = document.getElementById('moderation-watch');
    watch.hidden = !video.watch_url;
    if (video.watch_url) watch.href = video.watch_url; else watch.removeAttribute('href');
  }
  function showDraft() {
    if (videoForm) return;
    const draft = document.getElementById('moderation-draft');
    draft.hidden = !closed || !form.elements.reason.value.length;
    document.getElementById('moderation-draft-choice').textContent = 'Выбранное решение: ' + (form.elements.status.value === 'resolved' ? 'Решена' : 'Отклонена');
    document.getElementById('moderation-draft-reason').textContent = form.elements.reason.value;
  }
  function renderReport(report) {
    closed = report.status !== 'open'; form.hidden = closed;
    document.getElementById('report-state').textContent = report.status;
    document.getElementById('report-decision').textContent = report.decision_reason || '';
    document.getElementById('report-decider').textContent = report.decided_by ? 'Рассмотрел: ' + report.decided_by : '';
    renderVideo(report.video); showDraft();
  }
  function concealVideo() {
    card.hidden = true;
    const watch = document.getElementById('moderation-watch'); watch.hidden = true; watch.removeAttribute('href');
  }
  async function refreshState(keepError = false) {
    if (refreshing) return;
    refreshing = true; controls(); refreshStatus.textContent = 'Получаем актуальные сведения…';
    if (!keepError) { error.textContent = ''; success.textContent = ''; }
    try {
      const data = await apiRead(profile, validate);
      if (videoForm) renderVideo(data); else renderReport(data);
      forbidden = false; needsRefresh = false;
      refreshStatus.textContent = 'Сведения обновлены с сервера. Введённый текст сохранён.';
    } catch (failure) {
      needsRefresh = true; concealVideo();
      if ([401, 403].includes(failure.status)) forbidden = true;
      refreshStatus.textContent = 'Актуальные сведения не получены. Текст сохранён; нажмите «Обновить сведения», чтобы повторить.';
      if (!keepError) error.textContent = failure instanceof TypeError ? 'Не удалось связаться с сервером.' : failure.message;
    } finally { refreshing = false; controls(); }
  }
  refresh.addEventListener('click', () => refreshState());
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || refreshing || forbidden || closed || needsRefresh) return;
    const payload = Object.fromEntries(new FormData(form));
    if (videoForm) {
      payload.status = event.submitter?.value;
      if (!payload.status) { error.textContent = 'Выберите действие кнопкой «Скрыть» или «Восстановить».'; return; }
    }
    error.textContent = ''; success.textContent = ''; refreshStatus.textContent = ''; busy = true; controls();
    try {
      const validResult = data => videoForm ? ModerationData.result(data, videoId) && data.moderation_status === payload.status :
        validate(data) && data.status === payload.status;
      const result = await apiRequest(profile + (videoForm ? '/visibility' : '/decision'), 'POST', payload, validResult);
      if (videoForm) {
        renderVideo(result);
        if (result.changed) {
          form.elements.reason.value = '';
          success.textContent = result.moderation_status === 'hidden' ? 'Видео скрыто. Действие сохранено в журнале.' : 'Видео восстановлено. Действие сохранено в журнале.';
        } else {
          success.textContent = (result.moderation_status === 'hidden' ? 'Видео уже скрыто.' : 'Видео уже восстановлено.') + ' Новая запись в журнал не добавлена. Введённый текст сохранён.';
        }
      } else {
        form.elements.reason.value = ''; renderReport(result);
        success.textContent = 'Решение сохранено в журнале.';
      }
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? 'Не удалось подтвердить действие. Проверьте актуальные сведения перед повтором.' : failure.message;
      if ([401, 403].includes(failure.status)) { forbidden = true; concealVideo(); }
      if (!videoForm && failure.code === 'report_closed') {
        closed = true; form.hidden = true; showDraft();
        document.getElementById('report-state').textContent = 'Жалоба уже рассмотрена';
      }
      if (videoForm && failure.code === 'video_deleted') form.dataset.deleted = 'true';
      if (failure instanceof TypeError || ['invalid_response', 'request_timeout'].includes(failure.code) || failure.status === 404 || failure.status === 409 || failure.status >= 500) {
        needsRefresh = true; concealVideo(); await refreshState(true);
      }
    } finally { busy = false; controls(); }
  });
  controls();
})();
