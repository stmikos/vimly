(function(){
  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.expand();
    tg.ready();
    tg.MainButton.hide();
    document.body.style.backgroundColor = tg.themeParams.bg_color || "#0d1018";
  }

  const form = document.getElementById('quiz');
  form.addEventListener('submit', function(e){
    e.preventDefault();
    const company = (document.getElementById('company').value||"").trim();
    const task = (document.getElementById('task').value||"").trim();
    const contact = (document.getElementById('contact').value||"").trim();
    if (!company || !task || !contact) {
      alert("Заполните все поля");
      return;
    }
    const payload = {
      type: "webapp_lead",
      company, task, contact,
      ts: new Date().toISOString(),
      brand: "Vimly"
    };
    try {
      tg?.sendData(JSON.stringify(payload));
      if (tg?.HapticFeedback) tg.HapticFeedback.impactOccurred("heavy");
      const btn = document.getElementById('send');
      btn.textContent = "Отправлено ✓";
      btn.disabled = true;
      setTimeout(()=> { tg?.close(); }, 500);
    } catch (e) {
      console.error(e);
      alert("Не удалось отправить. Попробуйте ещё раз.");
    }
  });
})();