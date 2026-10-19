/**
 * MEDIA & SPOTIFY DOWNLOADER PRO - JAVASCRIPT CONTROLLER
 */

let currentSpotifyData = null;
let currentYtData = null;
let currentYtMediaType = 'audio';
let activeEventSource = null;
let libraryFilesCache = [];

// Recuerda para que tipo de medio se armo el desplegable de calidad.
// Sin esto, selectYtMediaType() reconstruia el <select> con innerHTML en CADA
// llamada, y como se llama en cada tecla que escribis en el campo de la URL,
// la calidad que habias elegido volvia sola al valor por defecto.
let ultimoTipoCalidadRenderizado = null;

// Tarea de descarga en curso, para poder cancelarla desde la interfaz.
let currentTaskId = null;
let currentTaskPrefix = null;


/* =====================================================================
   CANCELACION DE DESCARGAS
   ===================================================================== */
function mostrarBotonCancelar(prefix, visible) {
  const fila = document.getElementById(prefix === 'yt' ? 'ytCancelRow' : 'spotifyCancelRow');
  if (fila) fila.style.display = visible ? 'block' : 'none';
}

async function cancelCurrentTask() {
  if (!currentTaskId) return;

  try {
    const res = await fetch(`/api/tasks/${currentTaskId}/cancel`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast('Cancelando descarga...', 'info');
    } else {
      showToast('No se pudo cancelar la descarga.', 'error');
    }
  } catch (err) {
    showToast('Error al cancelar: ' + err.message, 'error');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  loadConfigAndDiagnostics();
  loadLibraryFiles();
  initAudioPlayer();
  initYtInputListeners();
});

function initYtInputListeners() {
  const input = document.getElementById('ytUrlInput');
  if (input) {
    input.addEventListener('input', () => {
      selectYtMediaType(currentYtMediaType);
    });
  }
}

/* =====================================================================
   TAB NAVIGATION
   ===================================================================== */
function initTabs() {
  const tabs = document.querySelectorAll('.tab-btn');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.getAttribute('data-tab');
      switchTab(target);
    });
  });
}

function switchTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(b => {
    const isTarget = b.getAttribute('data-tab') === tabId;
    b.classList.toggle('active', isTarget);
    b.setAttribute('aria-selected', isTarget);
  });

  document.querySelectorAll('.tab-content').forEach(panel => {
    panel.classList.toggle('active', panel.id === tabId);
  });

  if (tabId === 'tab-library') {
    loadLibraryFiles();
  }
}

/* =====================================================================
   CLIPBOARD & TOASTS
   ===================================================================== */
async function pasteClipboard(targetInputId) {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      document.getElementById(targetInputId).value = text.trim();
      showToast('Enlace pegado desde el portapapeles', 'info');
      if (targetInputId === 'ytUrlInput') {
        selectYtMediaType(currentYtMediaType);
      }
    }
  } catch (err) {
    showToast('No se pudo acceder al portapapeles. Pega con Ctrl+V.', 'error');
  }
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  
  let icon = 'ℹ️';
  if (type === 'success') icon = '✅';
  if (type === 'error') icon = '❌';

  // Antes: toast.innerHTML = `<span>${icon}</span> <div>${message}</div>`;
  // Se comento porque 'message' llega desde el servidor (errores de yt-dlp,
  // de Spotify o nombres de archivo). Si el texto contenia HTML, se ejecutaba
  // en la pagina. El resto del codigo ya escapaba con escapeHtml(); esto era
  // la excepcion. Con textContent el navegador lo trata siempre como texto.
  const iconSpan = document.createElement('span');
  iconSpan.textContent = icon;

  const msgDiv = document.createElement('div');
  msgDiv.textContent = message;

  toast.appendChild(iconSpan);
  toast.appendChild(msgDiv);
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(40px)';
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

/* =====================================================================
   CONFIG & DIAGNOSTICS
   ===================================================================== */
async function loadConfigAndDiagnostics() {
  try {
    const [cfgRes, diagRes] = await Promise.all([
      fetch('/api/config').then(r => r.json()),
      fetch('/api/diagnostics').then(r => r.json())
    ]);

    // Actualizar Badges del Header
    const spotifyBadge = document.getElementById('spotifyStatusBadge');
    const spotifyText = document.getElementById('spotifyStatusText');
    const spotifyNotice = document.getElementById('spotifyApiNotice');

    if (cfgRes.has_spotify_credentials) {
      if (spotifyBadge) spotifyBadge.className = 'badge-status';
      if (spotifyText) spotifyText.textContent = 'Spotify Conectado';
      if (spotifyNotice) spotifyNotice.style.display = 'none';
    } else {
      if (spotifyBadge) spotifyBadge.className = 'badge-status warning';
      if (spotifyText) spotifyText.textContent = 'Spotify Sin Clave';
      if (spotifyNotice) spotifyNotice.style.display = 'block';
    }

    const ffmpegBadge = document.getElementById('ffmpegStatusBadge');
    const ffmpegText = document.getElementById('ffmpegStatusText');
    if (diagRes.ffmpeg_installed) {
      if (ffmpegBadge) ffmpegBadge.className = 'badge-status';
      if (ffmpegText) ffmpegText.textContent = 'FFmpeg Listo';
    } else {
      if (ffmpegBadge) ffmpegBadge.className = 'badge-status warning';
      if (ffmpegText) ffmpegText.textContent = 'FFmpeg Faltante';
    }

    // Los botones "Abrir en Explorador" abren una carpeta en la maquina donde
    // corre el SERVIDOR. Si entras desde el celular o desde un deploy remoto
    // no hacen nada util, asi que se ocultan.
    if (!diagRes.is_local_request) {
      document.querySelectorAll('[onclick="openWindowsFolder()"]').forEach(btn => {
        btn.style.display = 'none';
      });
    }
  } catch (err) {
    console.error('Error cargando diagnósticos:', err);
  }
}

/* =====================================================================
   YOUTUBE & VIDEO DOWNLOADER
   ===================================================================== */
async function analyzeYouTubeUrl() {
  const url = document.getElementById('ytUrlInput').value.trim();
  if (!url) {
    showToast('Por favor ingresa o pega un enlace de YouTube.', 'error');
    return;
  }

  const btn = document.getElementById('btnAnalyzeYt');
  btn.disabled = true;
  btn.textContent = 'Analizando...';

  try {
    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();

    if (!res.ok || !data.success) {
      showToast(data.error || 'No se pudo obtener información del video.', 'error');
      return;
    }

    currentYtData = data;
    renderYouTubePreview(data);
    showToast('¡Información del video obtenida!', 'success');
  } catch (err) {
    showToast('Error al analizar URL: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    // Antes decia '🔍 Analizar Enlace', que no coincide con el texto original
    // del boton en el HTML ('🔍 Previsualizar'): despues de analizar una vez,
    // el boton cambiaba de nombre solo y ya no volvia al original.
    // btn.textContent = '🔍 Analizar Enlace';
    btn.textContent = '🔍 Previsualizar';
    selectYtMediaType(currentYtMediaType);
  }
}

function renderYouTubePreview(data) {
  document.getElementById('ytTitle').textContent = data.title;
  document.getElementById('ytUploader').textContent = data.subtitle || 'Desconocido';
  document.getElementById('ytDuration').textContent = data.duration_str || '00:00';
  document.getElementById('ytThumb').src = data.cover_image || '';
  document.getElementById('ytViews').textContent = data.view_count ? `👁️ ${data.view_count} vistas` : '⚡ Descarga Directa';

  selectYtMediaType(currentYtMediaType || 'audio');
  document.getElementById('ytPreview').classList.add('active');
  document.getElementById('ytProgressCard').classList.remove('active');
  
  // Desplazar suavemente hacia la vista previa
  document.getElementById('ytPreview').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function selectYtMediaType(type) {
  currentYtMediaType = type;
  
  // Elementos Main y Preview
  const optAudioMain = document.getElementById('optYtAudioMain');
  const optVideoMain = document.getElementById('optYtVideoMain');
  const optAudioPrev = document.getElementById('optYtAudioPreview');
  const optVideoPrev = document.getElementById('optYtVideoPreview');
  const qualitySelect = document.getElementById('ytQualitySelect');
  const quickBtn = document.getElementById('btnQuickDownloadYt');
  const previewBtn = document.getElementById('btnStartDownloadYt');

  if (type === 'audio') {
    if (optAudioMain) { optAudioMain.className = 'option-box selected audio'; }
    if (optVideoMain) { optVideoMain.className = 'option-box'; }
    if (optAudioPrev) { optAudioPrev.className = 'option-box selected audio'; }
    if (optVideoPrev) { optVideoPrev.className = 'option-box'; }

    // Solo se reconstruye si REALMENTE cambio el tipo de medio.
    // Antes se reescribia siempre y pisaba la eleccion del usuario.
    if (qualitySelect && ultimoTipoCalidadRenderizado !== 'audio') {
      qualitySelect.innerHTML = `
        <option value="320" selected>MP3 Ultra HD (320 kbps)</option>
        <option value="256">MP3 Alta Calidad (256 kbps)</option>
        <option value="192">MP3 Estándar (192 kbps)</option>
        <option value="128">MP3 Ligero (128 kbps)</option>
      `;
      ultimoTipoCalidadRenderizado = 'audio';
    }

    if (quickBtn) {
      quickBtn.disabled = false;
      quickBtn.className = 'btn btn-primary';
      quickBtn.style.background = 'linear-gradient(135deg, #10b981 0%, #059669 100%)';
      quickBtn.innerHTML = '⬇️ Descargar MP3 Directo';
    }
    if (previewBtn) {
      previewBtn.disabled = false;
      previewBtn.innerHTML = '⬇️ Descargar MP3 Ahora';
    }
  } else {
    if (optVideoMain) { optVideoMain.className = 'option-box selected video'; }
    if (optAudioMain) { optAudioMain.className = 'option-box'; }
    if (optVideoPrev) { optVideoPrev.className = 'option-box selected video'; }
    if (optAudioPrev) { optAudioPrev.className = 'option-box'; }

    // Misma logica que en audio: reconstruir solo al cambiar de tipo.
    if (qualitySelect && ultimoTipoCalidadRenderizado !== 'video') {
      qualitySelect.innerHTML = `
        <option value="best" selected>MP4 Mejor Calidad (1080p / Auto)</option>
        <option value="720">MP4 Alta Definición (720p)</option>
        <option value="480">MP4 Definición Estándar (480p)</option>
      `;
      ultimoTipoCalidadRenderizado = 'video';
    }

    if (quickBtn) {
      quickBtn.disabled = false;
      quickBtn.className = 'btn btn-yt';
      quickBtn.style.background = '';
      quickBtn.innerHTML = '⬇️ Descargar MP4 Directo';
    }
    if (previewBtn) {
      previewBtn.disabled = false;
      previewBtn.innerHTML = '⬇️ Descargar MP4 Ahora';
    }
  }

  updateSelectedFormatTag();
}

function updateSelectedFormatTag() {
  const tag = document.getElementById('selectedFormatTag');
  const qualitySelect = document.getElementById('ytQualitySelect');
  if (!tag || !qualitySelect) return;

  const selectedText = qualitySelect.options[qualitySelect.selectedIndex]?.text || '';
  if (currentYtMediaType === 'audio') {
    tag.style.color = '#1ed760';
    tag.textContent = `🎵 Audio MP3 (${selectedText})`;
  } else {
    tag.style.color = '#ff4d79';
    tag.textContent = `🎬 Video MP4 (${selectedText})`;
  }
}

async function startYouTubeDownload() {
  const url = document.getElementById('ytUrlInput').value.trim();
  if (!url) {
    showToast('Por favor pega primero un enlace de YouTube.', 'error');
    document.getElementById('ytUrlInput').focus();
    return;
  }

  const qualitySelect = document.getElementById('ytQualitySelect');
  const qualityVal = qualitySelect ? qualitySelect.value : '320';
  const quickBtn = document.getElementById('btnQuickDownloadYt');
  const previewBtn = document.getElementById('btnStartDownloadYt');

  if (quickBtn) { quickBtn.disabled = true; quickBtn.textContent = 'Iniciando descarga...'; }
  if (previewBtn) { previewBtn.disabled = true; previewBtn.textContent = 'Iniciando descarga...'; }

  // Mostrar Card de Progreso
  const progressCard = document.getElementById('ytProgressCard');
  progressCard.classList.add('active');
  document.getElementById('ytCompletedActions').classList.remove('active');
  document.getElementById('ytProgressBar').style.width = '5%';
  document.getElementById('ytProgressPct').textContent = '5%';
  document.getElementById('ytProgressTitle').textContent = `Conectando servidor (${currentYtMediaType.toUpperCase()})...`;

  // Desplazar hacia la barra de progreso
  progressCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  try {
    const payload = {
      url: url,
      media_type: currentYtMediaType,
      audio_bitrate: currentYtMediaType === 'audio' ? qualityVal : '320',
      video_quality: currentYtMediaType === 'video' ? qualityVal : 'best'
    };

    const res = await fetch('/api/download/youtube', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    if (!data.success) {
      showToast(data.detail || 'Error al iniciar descarga', 'error');
      selectYtMediaType(currentYtMediaType);
      return;
    }

    // Escuchar progreso en tiempo real vía SSE
    listenToTaskProgress(data.task_id, 'yt', () => {
      selectYtMediaType(currentYtMediaType);
      loadLibraryFiles();
    });

  } catch (err) {
    showToast('Error al conectar: ' + err.message, 'error');
    selectYtMediaType(currentYtMediaType);
  }
}

/* =====================================================================
   SPOTIFY DOWNLOADER
   ===================================================================== */
async function analyzeSpotifyUrl() {
  const url = document.getElementById('spotifyUrlInput').value.trim();
  if (!url) return;

  const btn = document.getElementById('btnAnalyzeSpotify');
  btn.disabled = true;
  btn.textContent = 'Obteniendo...';

  try {
    const res = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();

    if (!res.ok || !data.success) {
      if (data.needs_credentials) {
        document.getElementById('spotifyApiNotice').style.display = 'block';

        // BUG: switchTab('tab-settings') apuntaba a una pestaña que NO existe
        // en el HTML. switchTab desactiva todos los paneles y activa el que
        // coincide; como ninguno coincidia, la pagina entera quedaba en blanco
        // y habia que recargar. Se comento: con mostrar el aviso alcanza.
        // switchTab('tab-settings');

        // Llevamos la vista hasta el aviso para que no pase desapercibido.
        document.getElementById('spotifyApiNotice').scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      showToast(data.error || 'No se pudo obtener información de Spotify.', 'error');
      return;
    }

    currentSpotifyData = data;
    renderSpotifyPreview(data);
    showToast(`¡Colección cargada con ${data.total_tracks} canciones!`, 'success');
  } catch (err) {
    showToast('Error al analizar URL de Spotify: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '🔍 Obtener Canciones';
  }
}

function renderSpotifyPreview(data) {
  document.getElementById('spotifyTitle').textContent = data.title;
  document.getElementById('spotifySubtitle').textContent = data.subtitle || 'Spotify Collection';
  document.getElementById('spotifyCount').textContent = `🎵 ${data.total_tracks} canciones`;
  document.getElementById('spotifyCover').src = data.cover_image || '';

  // Renderizar tabla de canciones
  const tbody = document.getElementById('spotifyTracksBody');
  tbody.innerHTML = '';

  data.tracks.forEach((track, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="checkbox" class="track-checkbox track-item-chk" data-idx="${i}" checked onchange="updateSelectedSpotifyCount()"></td>
      <td>${track.index || (i + 1)}</td>
      <td style="font-weight: 600; color: #fff;">${escapeHtml(track.title)}</td>
      <td style="color: var(--text-secondary);">${escapeHtml(track.artists)}</td>
      <td style="color: var(--text-muted); font-size: 0.8rem;">${escapeHtml(track.album || '--')}</td>
      <td style="text-align: right; color: var(--text-muted);">${track.duration_str || '--'}</td>
    `;
    tbody.appendChild(tr);
  });

  updateSelectedSpotifyCount();
  document.getElementById('spotifyPreview').classList.add('active');
  document.getElementById('spotifyProgressCard').classList.remove('active');
}

function toggleSelectAllTracks(checked) {
  document.querySelectorAll('.track-item-chk').forEach(chk => {
    chk.checked = checked;
  });
  document.getElementById('chkMasterTracks').checked = checked;
  updateSelectedSpotifyCount();
}

function updateSelectedSpotifyCount() {
  const selected = document.querySelectorAll('.track-item-chk:checked').length;
  document.getElementById('selectedTracksCount').textContent = selected;
  const btn = document.getElementById('btnStartDownloadSpotify');
  btn.textContent = `⬇️ Descargar ${selected} Canciones Seleccionadas`;
  btn.disabled = selected === 0;
}

async function startSpotifyDownload() {
  if (!currentSpotifyData) return;

  const checkedCheckboxes = Array.from(document.querySelectorAll('.track-item-chk:checked'));
  const selectedTracks = checkedCheckboxes.map(chk => {
    const idx = parseInt(chk.getAttribute('data-idx'));
    return currentSpotifyData.tracks[idx];
  });

  if (selectedTracks.length === 0) {
    showToast('Selecciona al menos una canción para descargar.', 'error');
    return;
  }

  const asZip = document.getElementById('chkAsZip').checked;
  const bitrate = document.getElementById('spotifyBitrateSelect').value;
  const btn = document.getElementById('btnStartDownloadSpotify');

  btn.disabled = true;
  btn.textContent = 'Iniciando descarga por lotes...';

  // Mostrar Card de Progreso Spotify
  const progressCard = document.getElementById('spotifyProgressCard');
  progressCard.classList.add('active');
  document.getElementById('spotifyCompletedActions').classList.remove('active');
  document.getElementById('spotifyProgressBar').style.width = '0%';
  document.getElementById('spotifyProgressPct').textContent = '0%';
  document.getElementById('spotifyProgressTitle').textContent = `Iniciando colección: ${currentSpotifyData.title}...`;

  try {
    const payload = {
      tracks: selectedTracks,
      collection_name: currentSpotifyData.title,
      as_zip: asZip,
      audio_bitrate: bitrate
    };

    const res = await fetch('/api/download/spotify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    if (!data.success) {
      showToast(data.detail || 'Error al iniciar descarga de Spotify', 'error');
      btn.disabled = false;
      updateSelectedSpotifyCount();
      return;
    }

    listenToTaskProgress(data.task_id, 'spotify', () => {
      btn.disabled = false;
      updateSelectedSpotifyCount();
      loadLibraryFiles();
    });

  } catch (err) {
    showToast('Error: ' + err.message, 'error');
    btn.disabled = false;
    updateSelectedSpotifyCount();
  }
}

/* =====================================================================
   SSE REAL-TIME PROGRESS LISTENER
   ===================================================================== */
function listenToTaskProgress(taskId, prefix, onComplete) {
  if (activeEventSource) {
    activeEventSource.close();
  }

  const eventSource = new EventSource(`/api/tasks/${taskId}/events`);
  activeEventSource = eventSource;

  // Guardamos la tarea en curso para que el boton de cancelar sepa a cual apuntar.
  currentTaskId = taskId;
  currentTaskPrefix = prefix;
  mostrarBotonCancelar(prefix, true);

  // Resetear estado visual de inicio
  if (prefix === 'yt') {
    const icon = document.getElementById('ytProgressIcon');
    if (icon) icon.textContent = '⏳';
  } else {
    const icon = document.getElementById('spotifyProgressIcon');
    if (icon) icon.textContent = '⏳';
  }

  eventSource.onmessage = (event) => {
    const task = JSON.parse(event.data);
    const progress = Math.min(100, Math.max(0, task.progress || 0));

    if (prefix === 'yt') {
      const pBar = document.getElementById('ytProgressBar');
      const pPct = document.getElementById('ytProgressPct');
      const pTitle = document.getElementById('ytProgressTitle');
      const pSpeed = document.getElementById('ytProgressSpeed');
      const pEta = document.getElementById('ytProgressEta');

      if (pBar) pBar.style.width = `${progress}%`;
      if (pPct) pPct.textContent = `${progress}%`;
      if (pTitle) pTitle.textContent = task.title || 'Descargando...';
      if (pSpeed) pSpeed.textContent = `Velocidad: ${task.speed || '--'}`;
      if (pEta) pEta.textContent = `Tiempo restante: ${task.eta || '--'}`;
    } else {
      const pBar = document.getElementById('spotifyProgressBar');
      const pPct = document.getElementById('spotifyProgressPct');
      const pTitle = document.getElementById('spotifyProgressTitle');
      const pItem = document.getElementById('spotifyCurrentItem');
      const pCount = document.getElementById('spotifyItemCount');

      if (pBar) pBar.style.width = `${progress}%`;
      if (pPct) pPct.textContent = `${progress}%`;
      if (pTitle) pTitle.textContent = task.title || 'Procesando colección...';
      if (pItem) pItem.textContent = task.current_item || 'Buscando pistas en alta fidelidad...';
      if (pCount) pCount.textContent = `${task.completed_items || 0}/${task.total_items || 0} pistas`;
    }

    if (task.status === 'completed') {
      eventSource.close();
      mostrarBotonCancelar(prefix, false);
      currentTaskId = null;
      showToast('🎉 ¡Descarga completada con éxito!', 'success');

      if (prefix === 'yt') {
        const icon = document.getElementById('ytProgressIcon');
        const pTitle = document.getElementById('ytProgressTitle');
        const pSpeed = document.getElementById('ytProgressSpeed');
        const pEta = document.getElementById('ytProgressEta');
        const pBar = document.getElementById('ytProgressBar');
        const pPct = document.getElementById('ytProgressPct');

        if (icon) icon.textContent = '✅';
        if (pTitle) pTitle.textContent = `✅ ${task.title || '¡Descarga completada!'}`;
        if (pSpeed) pSpeed.textContent = '✔️ Finalizado correctamente';
        if (pEta) pEta.textContent = '📁 Guardado en tu biblioteca';
        if (pBar) pBar.style.width = '100%';
        if (pPct) pPct.textContent = '100%';

        const dlLink = document.getElementById('ytDirectDownloadLink');
        if (dlLink) {
          dlLink.href = task.download_url;
          dlLink.download = task.file_name || 'archivo';
        }
        const actBox = document.getElementById('ytCompletedActions');
        if (actBox) actBox.classList.add('active');

        // Reactivar botones
        selectYtMediaType(currentYtMediaType);
      } else {
        const icon = document.getElementById('spotifyProgressIcon');
        const pTitle = document.getElementById('spotifyProgressTitle');
        const pItem = document.getElementById('spotifyCurrentItem');
        const pBar = document.getElementById('spotifyProgressBar');
        const pPct = document.getElementById('spotifyProgressPct');

        if (icon) icon.textContent = '✅';
        if (pTitle) pTitle.textContent = `✅ ${task.title || '¡Colección completada!'}`;
        if (pItem) pItem.textContent = '✔️ Todas las pistas descargadas y empaquetadas';
        if (pBar) pBar.style.width = '100%';
        if (pPct) pPct.textContent = '100%';

        const dlLink = document.getElementById('spotifyDirectDownloadLink');
        if (dlLink) {
          if (task.download_url) {
            dlLink.href = task.download_url;
            dlLink.download = task.file_name || 'playlist.zip';
            dlLink.style.display = '';
          } else {
            // Descarga sin ZIP: no hay un archivo unico que guardar, los MP3
            // quedan sueltos en la Biblioteca. Antes el link apuntaba a la
            // carpeta de la coleccion y al tocarlo daba 404.
            dlLink.style.display = 'none';
          }
        }
        const actBox = document.getElementById('spotifyCompletedActions');
        if (actBox) actBox.classList.add('active');

        const spotifyBtn = document.getElementById('btnStartDownloadSpotify');
        if (spotifyBtn) {
          spotifyBtn.disabled = false;
          updateSelectedSpotifyCount();
        }
      }

      if (onComplete) onComplete();
    } else if (task.status === 'cancelled') {
      // Rama nueva: antes el estado 'cancelled' no se manejaba. El servidor
      // cerraba el stream y la interfaz quedaba con los botones deshabilitados
      // y la barra congelada hasta recargar la pagina.
      eventSource.close();
      mostrarBotonCancelar(prefix, false);
      currentTaskId = null;
      showToast('Descarga cancelada.', 'info');

      const icon = document.getElementById(prefix === 'yt' ? 'ytProgressIcon' : 'spotifyProgressIcon');
      const pTitle = document.getElementById(prefix === 'yt' ? 'ytProgressTitle' : 'spotifyProgressTitle');
      const pBar = document.getElementById(prefix === 'yt' ? 'ytProgressBar' : 'spotifyProgressBar');
      const pPct = document.getElementById(prefix === 'yt' ? 'ytProgressPct' : 'spotifyProgressPct');

      if (icon) icon.textContent = '⛔';
      if (pTitle) pTitle.textContent = 'Descarga cancelada por el usuario';
      if (pBar) pBar.style.width = '0%';
      if (pPct) pPct.textContent = '0%';

      if (prefix === 'yt') {
        selectYtMediaType(currentYtMediaType);
      } else {
        const spotifyBtn = document.getElementById('btnStartDownloadSpotify');
        if (spotifyBtn) {
          spotifyBtn.disabled = false;
          updateSelectedSpotifyCount();
        }
      }

      if (onComplete) onComplete();

    } else if (task.status === 'error') {
      eventSource.close();
      mostrarBotonCancelar(prefix, false);
      currentTaskId = null;
      showToast('❌ Error en la descarga: ' + (task.error || 'Error desconocido'), 'error');

      if (prefix === 'yt') {
        const icon = document.getElementById('ytProgressIcon');
        const pSpeed = document.getElementById('ytProgressSpeed');
        const pEta = document.getElementById('ytProgressEta');
        if (icon) icon.textContent = '❌';
        if (pSpeed) pSpeed.textContent = 'Error en descarga';
        if (pEta) pEta.textContent = 'Revisa la URL o inténtalo de nuevo';
        selectYtMediaType(currentYtMediaType);
      } else {
        const icon = document.getElementById('spotifyProgressIcon');
        if (icon) icon.textContent = '❌';
        const spotifyBtn = document.getElementById('btnStartDownloadSpotify');
        if (spotifyBtn) {
          spotifyBtn.disabled = false;
          updateSelectedSpotifyCount();
        }
      }

      if (onComplete) onComplete();
    }
  };

  eventSource.onerror = () => {
    eventSource.close();
    mostrarBotonCancelar(prefix, false);
    if (prefix === 'yt') {
      selectYtMediaType(currentYtMediaType);
    } else {
      const spotifyBtn = document.getElementById('btnStartDownloadSpotify');
      if (spotifyBtn) {
        spotifyBtn.disabled = false;
        updateSelectedSpotifyCount();
      }
    }
  };
}

/* =====================================================================
   LIBRARY & FILE MANAGER
   ===================================================================== */
async function loadLibraryFiles() {
  try {
    const res = await fetch('/api/files');
    const data = await res.json();
    libraryFilesCache = data.files || [];
    renderLibraryGrid(libraryFilesCache);
  } catch (err) {
    console.error('Error cargando biblioteca:', err);
  }
}

function renderLibraryGrid(files) {
  const grid = document.getElementById('libraryGrid');
  const emptyState = document.getElementById('libraryEmptyState');

  if (!files || files.length === 0) {
    grid.innerHTML = '';
    emptyState.style.display = 'block';
    return;
  }

  emptyState.style.display = 'none';
  grid.innerHTML = '';

  files.forEach(file => {
    const card = document.createElement('div');
    card.className = 'library-item-card';

    const ext = file.extension.toLowerCase();
    const isAudio = ext === 'mp3' || ext === 'm4a' || ext === 'wav';
    let iconClass = 'mp3';
    let iconText = '🎵';

    if (ext === 'mp4' || ext === 'webm') {
      iconClass = 'mp4';
      iconText = '🎬';
    } else if (ext === 'zip') {
      iconClass = 'zip';
      iconText = '📦';
    }

    card.innerHTML = `
      <div style="display: flex; gap: 12px; align-items: flex-start;">
        <div class="file-type-icon ${iconClass}">${iconText}</div>
        <div style="flex: 1; overflow: hidden;">
          <div class="file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</div>
          <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 2px;">
            ${file.subfolder ? '📁 ' + escapeHtml(file.subfolder) : 'Descarga directa'}
          </div>
        </div>
      </div>

      <div class="file-meta-row">
        <span>Tamaño: <strong>${file.size_str}</strong></span>
        <span>Formato: <strong>${ext.toUpperCase()}</strong></span>
      </div>

      <div class="file-actions-row">
        ${isAudio ? `
          <button class="btn btn-secondary btn-sm btn-play-file" style="flex: 1;">
            ▶ Escuchar
          </button>
        ` : ''}
        <a href="${file.download_url}" class="btn btn-primary btn-sm" style="flex: 1;" download>
          📥 Bajar
        </a>
        <button class="btn btn-secondary btn-icon btn-sm btn-delete-file" title="Eliminar archivo">
          🗑️
        </button>
      </div>
    `;

    // Event listener seguro para reproducir audio
    const playBtn = card.querySelector('.btn-play-file');
    if (playBtn) {
      playBtn.addEventListener('click', () => {
        playAudio(file.stream_url, file.name);
      });
    }

    // Event listener seguro para eliminar archivo
    const delBtn = card.querySelector('.btn-delete-file');
    if (delBtn) {
      delBtn.addEventListener('click', () => {
        deleteLibraryFile(file.rel_path, file.name);
      });
    }

    grid.appendChild(card);
  });
}

function filterLibraryFiles() {
  const query = document.getElementById('librarySearchInput').value.toLowerCase().trim();
  if (!query) {
    renderLibraryGrid(libraryFilesCache);
    return;
  }
  const filtered = libraryFilesCache.filter(f => 
    f.name.toLowerCase().includes(query) ||
    f.extension.toLowerCase().includes(query) ||
    f.subfolder.toLowerCase().includes(query)
  );
  renderLibraryGrid(filtered);
}

let pendingDeleteAction = null;

function showConfirmModal(message, onConfirm) {
  const modal = document.getElementById('customConfirmModal');
  const msgElem = document.getElementById('customConfirmMsg');
  const okBtn = document.getElementById('customConfirmOkBtn');
  const cancelBtn = document.getElementById('customConfirmCancelBtn');

  msgElem.textContent = message;
  pendingDeleteAction = onConfirm;
  modal.classList.add('active');

  okBtn.onclick = async () => {
    modal.classList.remove('active');
    if (pendingDeleteAction) {
      await pendingDeleteAction();
      pendingDeleteAction = null;
    }
  };

  cancelBtn.onclick = () => {
    modal.classList.remove('active');
    pendingDeleteAction = null;
  };
}

async function deleteLibraryFile(relPath, fileName) {
  const nameToDisplay = fileName || relPath;
  
  showConfirmModal(`¿Estás seguro de que deseas eliminar permanentemente "${nameToDisplay}"?`, async () => {
    try {
      const res = await fetch('/api/files/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: relPath })
      });
      const data = await res.json();

      if (data.success) {
        showToast(`"${nameToDisplay}" eliminado correctamente`, 'info');
        loadLibraryFiles();
      } else {
        showToast(data.detail || 'Error al eliminar el archivo', 'error');
      }
    } catch (err) {
      showToast('Error de red al eliminar archivo: ' + err.message, 'error');
    }
  });
}

async function openWindowsFolder() {
  try {
    const res = await fetch('/api/files/open-folder', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast('Carpeta abierta en el explorador de archivos', 'success');
    } else {
      showToast('No se pudo abrir la carpeta automáticamente', 'error');
    }
  } catch (err) {
    showToast('Error: ' + err.message, 'error');
  }
}

/* =====================================================================
   FLOATING AUDIO PLAYER
   ===================================================================== */
let audioElem = null;

function initAudioPlayer() {
  audioElem = document.getElementById('globalAudioElement');
  const seekbar = document.getElementById('playerSeekbar');
  const playBtn = document.getElementById('playerPlayBtn');
  const curTime = document.getElementById('playerCurrentTime');
  const durTime = document.getElementById('playerDuration');

  audioElem.addEventListener('timeupdate', () => {
    if (audioElem.duration) {
      const pct = (audioElem.currentTime / audioElem.duration) * 100;
      seekbar.value = pct;
      curTime.textContent = formatSeconds(audioElem.currentTime);
      durTime.textContent = formatSeconds(audioElem.duration);
    }
  });

  audioElem.addEventListener('play', () => {
    playBtn.textContent = '⏸';
  });

  audioElem.addEventListener('pause', () => {
    playBtn.textContent = '▶';
  });

  audioElem.addEventListener('ended', () => {
    playBtn.textContent = '▶';
    seekbar.value = 0;
  });
}

function playAudio(streamUrl, title) {
  const bar = document.getElementById('audioPlayerBar');
  bar.classList.add('active');
  document.getElementById('playerTitle').textContent = title;

  audioElem.src = streamUrl;
  audioElem.play().catch(e => console.log('Autoplay bloqueado:', e));
}

function toggleAudioPlayback() {
  if (audioElem.paused) {
    audioElem.play();
  } else {
    audioElem.pause();
  }
}

function seekAudio(value) {
  if (audioElem.duration) {
    audioElem.currentTime = (value / 100) * audioElem.duration;
  }
}

function setAudioVolume(val) {
  if (audioElem) {
    audioElem.volume = parseFloat(val);
  }
}

function closeAudioPlayer() {
  if (audioElem) {
    audioElem.pause();
    audioElem.src = '';
  }
  document.getElementById('audioPlayerBar').classList.remove('active');
}

/* =====================================================================
   HELPERS
   ===================================================================== */
function formatSeconds(secs) {
  if (!secs || isNaN(secs)) return '00:00';
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m < 10 ? '0' : ''}${m}:${s < 10 ? '0' : ''}${s}`;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
