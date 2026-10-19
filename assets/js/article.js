// Progressive enhancement: article text and navigation work without JavaScript.
document.querySelectorAll('.prose pre').forEach((pre) => {
  pre.tabIndex = 0;
  pre.setAttribute('aria-label', 'Code example; scroll horizontally for long lines');
  if (!navigator.clipboard || !window.isSecureContext) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'copy-code';
  button.textContent = 'Copy code';
  button.setAttribute('aria-live', 'polite');
  let wrapper = pre.closest('div.highlighter-rouge');
  if (!wrapper) {
    wrapper = document.createElement('div');
    pre.before(wrapper);
    wrapper.append(pre);
  }
  wrapper.classList.add('code-container');
  wrapper.style.position = 'relative';
  wrapper.prepend(button);
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText((pre.querySelector('code') || pre).textContent);
      button.textContent = 'Copied';
    } catch {
      button.textContent = 'Select code to copy';
    }
    setTimeout(() => { button.textContent = 'Copy code'; }, 2200);
  });
});
