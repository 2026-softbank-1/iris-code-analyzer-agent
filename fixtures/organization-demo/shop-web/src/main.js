const apiUrl = import.meta.env.VITE_CATALOG_URL;
fetch(`${apiUrl}/catalog`).then(response => response.json()).then(items => {
  document.getElementById('catalog').textContent = JSON.stringify(items);
});
