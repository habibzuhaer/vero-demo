(() => {
  const vid = document.getElementById('hero-vid');
  const play = document.getElementById('hero-playback');
  const mute = document.getElementById('hero-mute');
  const box = document.querySelector('.float-vid');
  if (!vid || !play || !mute || !box) return;
  const sync = () => {
    box.classList.toggle('playing', !vid.paused);
    box.classList.toggle('unmuted', !vid.muted);
  };
  play.addEventListener('click', async () => { if (vid.paused) await vid.play(); else vid.pause(); sync(); });
  mute.addEventListener('click', async () => { vid.muted = !vid.muted; if (!vid.muted && vid.paused) await vid.play(); sync(); });
  vid.addEventListener('play', sync); vid.addEventListener('pause', sync);
  vid.play().catch(()=>{}); sync();
  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(btn.getAttribute('data-copy')||''); btn.textContent='copied'; setTimeout(()=>btn.textContent='copy',1100);} catch {}
    });
  });
})();
