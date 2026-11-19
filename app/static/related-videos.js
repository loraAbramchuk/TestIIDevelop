(() => {
  const panel = document.querySelector('[data-related-video]');
  if (!panel) return;
  const list = document.getElementById('related-videos');
  const status = document.getElementById('related-status');
  const retry = document.getElementById('related-retry');
  const duration = seconds => {
    const total = Math.max(1, Math.ceil(seconds));
    const minutes = Math.floor(total / 60), rest = String(total % 60).padStart(2, '0');
    return minutes >= 60 ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`;
  };
  function message(text) {
    status.replaceChildren();
    const paragraph = document.createElement('p'); paragraph.textContent = text;
    const link = document.createElement('a'); link.href = '/'; link.textContent = 'Открыть главную';
    status.append(paragraph, link); status.hidden = false;
  }
  async function load() {
    retry.hidden = true; retry.disabled = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch('/api/videos?limit=7', {signal: controller.signal, credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('catalog unavailable');
      const page = await response.json();
      if (!Array.isArray(page.items)) throw new Error('invalid catalog');
      const videos = page.items.filter(video => video.id !== panel.dataset.relatedVideo).slice(0, 6);
      const fragment = document.createDocumentFragment();
      for (const video of videos) {
        const row = document.createElement('article'); row.className = 'related-card';
        const preview = document.createElement('a'); preview.className = 'card-preview'; preview.href = '/watch/' + video.id;
        const image = document.createElement('img'); image.src = video.thumbnail_url; image.alt = 'Превью: ' + video.title; image.loading = 'lazy';
        const time = document.createElement('span'); time.className = 'card-duration'; time.textContent = duration(video.duration_seconds);
        preview.append(image, time);
        const copy = document.createElement('div'), heading = document.createElement('h3'), title = document.createElement('a');
        title.href = preview.href; title.textContent = video.title; heading.append(title);
        const author = document.createElement('a'); author.className = 'card-author'; author.href = '/channels/' + video.author.id; author.textContent = video.author.name;
        copy.append(heading, author); row.append(preview, copy); fragment.append(row);
      }
      list.replaceChildren(fragment);
      status.hidden = videos.length > 0;
      if (!videos.length) message('Новые видео авторов появятся здесь.');
    } catch {
      message('Не удалось загрузить другие видео. Попробуйте ещё раз.');
      retry.hidden = false;
    } finally { clearTimeout(timeout); retry.disabled = false; }
  }
  status.hidden = false;
  retry.addEventListener('click', load);
  load();
})();
