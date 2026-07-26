<!--
Brouillon pour le forum HACF (forum.hacf.fr) → catégorie « Vos projets / Partage ».
Non publié automatiquement. À copier/coller et ajuster (captures, liens) avant publication.
Titre suggéré : [Intégration custom] Éclairage Bluetooth Mesh (Häfele Connect
Mesh / ThingOS) — sans passerelle, via vos proxies ESPHome
-->

## Éclairage Bluetooth Mesh dans Home Assistant — sans passerelle vendeur

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
- Réponse instantanée (connexion proxy maintenue ; délai configurable si vous
  voulez aussi continuer à utiliser l'appli du fabricant — un nœud mesh n'a
  qu'un seul emplacement proxy).

### Limites, en toute transparence

- **Pas encore de RGB / couleur** : mon matériel est en blanc variable
  uniquement, donc j'ai préféré ne pas livrer de la couleur non testée. **Si
  vous avez une lampe mesh couleur et voulez aider à valider, faites signe** —
  c'est un ajout propre.
- La température de couleur n'est pas encore relue depuis la lampe : elle
  reflète la dernière commande. En revanche l'état allumé/éteint et la
  luminosité *sont* lus depuis le mesh — y compris les changements faits depuis
  l'appli pendant que HA était absent.

### Installation

HACS → dépôt personnalisé (catégorie *Intégration*) → installer → redémarrer →
ajouter l'intégration et coller votre export `.connect`. Détails et captures dans
le README.

Retours, testeurs et remontées bienvenus — surtout si vous avez des lampes
ThingOS autres que Häfele, ou du matériel couleur. 🙏
