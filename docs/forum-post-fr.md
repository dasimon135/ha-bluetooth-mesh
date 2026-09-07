<!--
Texte final pour forum.hacf.fr t/82123, post #1.

Fusionne la bannière d'entonnoir GitHub déjà en ligne (ajoutée le 2026-09-06)
avec un corps corrigé. Deux affirmations du post en ligne sont périmées :

* « État optimiste : les changements faits en parallèle depuis l'appli ne sont
  pas relus » est faux depuis la v0.2.0 (2026-07-26) — l'état EST relu, et le
  manifeste déclare `iot_class: local_polling` ;
* la température de couleur est lue depuis la lampe, avec sa vraie plage de
  Kelvin, depuis la v0.6.0 (2026-08-29).

C'est David qui édite le post ; rien ici n'est publié automatiquement.
-->

> ### 📍 Nouveautés et support : sur GitHub
>
> Je maintiens cette intégration seul et bénévolement, alors tout est suivi au même endroit : **[signaler un bug ou poser une question](https://github.com/dasimon135/ha-bluetooth-mesh/issues/new/choose)**.
>
> Les nouvelles versions n'ont plus besoin d'être annoncées ici : **HACS vous les propose**, notes de version comprises. Sinon, flux RSS `https://github.com/dasimon135/ha-bluetooth-mesh/releases.atom`, ou **Watch → Releases** sur le dépôt.
>
> Ce fil reste ouvert pour l'entraide. Le [README](https://github.com/dasimon135/ha-bluetooth-mesh#readme) fait foi et reste à jour — ce message, non.

Home Assistant ne gère pas nativement le **Bluetooth SIG Mesh**, ce qui laisse
sur le carreau des familles entières de lampes mesh « pilotables seulement par
l'appli » — dont **Häfele Connect Mesh (Loox)** et d'autres luminaires à base de
**ThingOS**. Les solutions habituelles : une passerelle vendeur (souvent
abandonnée), ou un montage BlueZ `bluetooth-meshd` expérimental qui ne tourne
pas sur Home Assistant OS.

J'ai donc écrit une petite **pile Bluetooth Mesh en pur Python** et une
**intégration custom (HACS)** qui pilotent ces lampes directement — **en
utilisant les proxies Bluetooth ESPHome que vous avez probablement déjà** (ou un
adaptateur local). Aucun matériel en plus, pas de meshd, fonctionne sur HA OS en
VM sans radio locale.

👉 **Dépôt :** https://github.com/dasimon135/ha-bluetooth-mesh

### Le principe

L'appli du fabricant exporte son réseau dans un fichier `.connect` (NetKey,
AppKey, adresses des nœuds). L'intégration l'importe, connecte un proxy au
**même** réseau, et envoie des messages mesh SIG **standard** (Generic OnOff,
Light Lightness, Light CTL). Une seule connexion GATT vers n'importe quelle lampe
alimentée suffit à joindre tout le mesh — le réseau relaie le reste.

Validé de bout en bout sur du vrai matériel : une lampe Häfele à blanc variable
pilotée depuis HA via un proxy ESPHome, avec une connexion maintenue qui rend
les commandes quasi instantanées.

### Ce qui marche aujourd'hui

- Marche/arrêt, **luminosité** et **température de couleur** (blanc variable),
  une entité `light` HA par nœud.
- **L'état est lu depuis la lampe, pas supposé.** Un proxy mesh ne transmet rien
  vers le client tant que celui-ci n'a pas configuré son filtre d'adresses ;
  l'intégration le configure, donc marche/arrêt, luminosité et température de
  couleur sont relus dès que le mesh redevient joignable et après chaque
  reconnexion — **y compris les changements faits depuis l'appli du fabricant
  pendant que Home Assistant était absent**. Une lampe dont l'état n'a pas encore
  été lu affiche `unknown` plutôt que de deviner `off`.
- La lampe est interrogée une fois sur la **plage de Kelvin qu'elle suit
  réellement**, pour que le curseur propose ses vraies extrémités au lieu d'un
  2700–6500 conventionnel.
- Réponse instantanée (connexion proxy maintenue ; délai configurable si vous
  voulez aussi continuer à utiliser l'appli du fabricant — un nœud mesh n'a
  qu'un seul emplacement proxy).

### Limites, en toute transparence

- **Pas encore de RGB / couleur** : mon matériel est en blanc variable
  uniquement, donc j'ai préféré ne pas livrer de la couleur non testée. **Si
  vous avez une lampe mesh couleur et voulez aider à valider, faites signe** —
  c'est un ajout propre.
- **L'appairage se fait toujours dans l'appli du fabricant.** L'intégration
  rejoint un réseau existant à partir de son export `.connect` ; ajouter une
  lampe neuve au mesh depuis Home Assistant n'est pas encore possible.

### Installation

HACS → dépôt personnalisé (catégorie *Intégration*) → installer → redémarrer →
ajouter l'intégration et coller votre export `.connect`. Détails et captures dans
le README.

Retours, testeurs et remontées bienvenus — surtout si vous avez des lampes
ThingOS autres que Häfele, ou du matériel couleur. 🙏
