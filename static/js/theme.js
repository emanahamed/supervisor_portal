/* Centralized Theme Utility (since 0.9.8)
 * Handles applying user/system theme preference with minimal FOUC.
 */
(function(){
  const STORAGE_KEY = 'portalTheme';
  const SERVER_PREF = (window.__SERVER_THEME_PREF__ || 'system').toLowerCase();
  function systemPref(){ return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
  function resolve(){
    const stored = localStorage.getItem(STORAGE_KEY);
    let mode = stored || SERVER_PREF || 'system';
    if(mode === 'system') mode = systemPref();
    return mode === 'dark' ? 'dark' : 'light';
  }
  function apply(mode){
    const root = document.documentElement;
    if(mode === 'dark') root.classList.add('dark'); else root.classList.remove('dark');
  }
  function init(){
    apply(resolve());
    // If either stored or server is system, respond to system changes
    const raw = localStorage.getItem(STORAGE_KEY) || SERVER_PREF;
    if(raw === 'system' && window.matchMedia){
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      mq.addEventListener('change', e => { if((localStorage.getItem(STORAGE_KEY)||SERVER_PREF)==='system'){ apply(e.matches?'dark':'light'); } });
    }
  }
  document.addEventListener('DOMContentLoaded', init);
  // Expose small API
  window.ThemeUtil = {
    set(mode){
      if(!['light','dark','system'].includes(mode)) mode = 'system';
      localStorage.setItem(STORAGE_KEY, mode);
      if(mode==='system') apply(systemPref()); else apply(mode);
    },
    current(){ return (document.documentElement.classList.contains('dark') ? 'dark' : 'light'); },
  };
})();
