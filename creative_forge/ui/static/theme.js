/* Creative Forge: the saved light/dark choice, applied before the page paints. A file, not an inline script:
   the Content-Security-Policy allows scripts from /static only. */
try{var s=localStorage.getItem('ui-theme');if(s)document.documentElement.setAttribute('data-theme',s)}catch(e){}
