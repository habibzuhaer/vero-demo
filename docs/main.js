(() => {
  const vid = document.getElementById('hero-vid');
  const play = document.getElementById('hero-playback');
  const mute = document.getElementById('hero-mute');
  const phone = document.querySelector('.phone');
  if (!vid || !play || !mute || !phone) return;

  const sync = () => {
    phone.classList.toggle('playing', !vid.paused);
    phone.classList.toggle('unmuted', !vid.muted);
    play.setAttribute('aria-label', vid.paused ? 'play video' : 'pause video');
    mute.setAttribute('aria-label', vid.muted ? 'unmute video' : 'mute video');
    mute.setAttribute('aria-pressed', String(vid.muted));
  };

  play.addEventListener('click', async () => {
    if (vid.paused) await vid.play(); else vid.pause();
    sync();
  });
  mute.addEventListener('click', async () => {
    vid.muted = !vid.muted;
    if (!vid.muted && vid.paused) await vid.play();
    sync();
  });
  vid.addEventListener('play', sync);
  vid.addEventListener('pause', sync);
  vid.play().catch(() => {});
  sync();

  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const text = btn.getAttribute('data-copy') || '';
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = 'copied';
        setTimeout(() => { btn.textContent = 'copy'; }, 1200);
      } catch {}
    });
  });
})();
