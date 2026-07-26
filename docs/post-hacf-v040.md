<!--
Brouillon de réponse dans le fil HACF existant :
https://forum.hacf.fr/t/integration-custom-eclairage-bluetooth-mesh-hafele-connect-mesh-thingos-sans-passerelle-via-vos-proxies-esphome/82123

⚠️ À faire AUSSI : éditer le post #1 du fil. Il contient encore « État optimiste :
les changements faits en parallèle depuis l'appli ne sont pas relus tant que HA
n'a pas envoyé sa commande suivante » — c'est faux depuis la v0.2.0.
Le texte corrigé est dans docs/forum-post-fr.md.
-->

Salut à tous 👋

Petit point depuis l'annonce. L'intégration est passée de la v0.1.0 à la
**v0.4.1**, et il y a un changement de fond que je veux expliquer, parce qu'il
change ce que vous voyez dans Home Assistant.

## Home Assistant sait enfin ce que font vraiment vos lampes

Jusqu'ici, l'état affiché était une supposition : HA vous montrait ce qu'il
avait demandé en dernier, pas ce que la lampe faisait. Vous éteigniez depuis
l'appli Häfele, HA continuait d'afficher « allumée ». Je pensais que la lampe
ne répondait simplement pas.

En fait si, elle répondait. Mais dans le Bluetooth Mesh, la lampe qui vous sert
de point d'entrée fait aussi office de standard téléphonique — et par défaut,
ce standard ne vous transmet **rien**. Il attend qu'on lui dise quelles adresses
on veut écouter. L'intégration ne le lui disait jamais, donc chaque réponse de
chaque lampe était jetée avant même de nous parvenir. Une conversation où on
parle tout seul sans le savoir.

Maintenant l'intégration le configure, et les réponses arrivent en **150 à
300 ms**. Concrètement :

- l'état allumé/éteint et la luminosité sont **lus sur la lampe** quand HA
  retrouve le réseau, et après chaque reconnexion — **y compris ce que vous avez
  changé depuis l'appli du fabricant pendant que HA n'était pas là** ;
- au redémarrage, HA ne réinvente plus un état : une lampe qu'il n'a pas encore
  interrogée s'affiche `inconnu`, pas « éteinte ».

Cette deuxième ligne a l'air d'un détail cosmétique. Elle ne l'est pas, et je
l'ai appris à mes dépens : chez moi, une lampe s'éteignait toute seule après
chaque redémarrage de HA. J'ai cherché longtemps. Le coupable était mon propre
code — l'entité naissait en annonçant « éteinte », une autre intégration
(Magic Areas, qui gère l'extinction automatique d'une pièce vide) prenait cette
valeur inventée pour argent comptant et éteignait la lampe pour de vrai.
L'invention devenait vraie. Et comme l'entité était déjà « éteinte », l'ordre
n'apparaissait nulle part dans l'historique. Invisible.

⚠️ Un seul point d'attention : si vous avez une automatisation qui teste
`state == 'off'` sur une de ces lampes, elle ne se déclenchera plus pendant le
court instant où l'état est `inconnu`, juste après un redémarrage.

## Deux bugs qui pouvaient bloquer l'intégration

Le premier laissait une connexion Bluetooth fantôme accrochée à la lampe.
Comme un nœud mesh n'accepte **qu'une seule** connexion à la fois, plus rien ne
pouvait s'y connecter ensuite — ni HA, ni l'appli du fabricant. Et l'intégration
se plaignait de ne pas trouver de proxy… alors que c'était elle qui bloquait la
place.

Le second était plus sournois : quand l'envoi tombait en panne, l'entité restait
affichée comme disponible, l'interface répondait normalement, et il ne se passait
strictement rien sur la lampe. Aucun message d'erreur. Il fallait recharger
l'intégration pour s'en sortir.

## Le reste

- **Bouton « Reconfigurer »** : vous avez ajouté une lampe et réexporté votre
  `.connect` ? Vous le recollez sur l'entrée existante, sans rien perdre.
  Avant, il fallait supprimer l'intégration et tout recréer — donc perdre les
  identifiants d'entités et l'historique.
- **Diagnostics** : le bouton « Télécharger les diagnostics » vous dit quel
  réseau l'intégration cherche, quels proxies mesh HA voit à cet instant, et la
  composition de vos nœuds. **Aucune clé de votre réseau dedans** — c'est vérifié
  par un test — donc vous pouvez le coller tel quel dans un rapport de bug.
- Un fichier d'export un peu abîmé ne fait plus échouer tout l'import, un réseau
  sans identifiant interne ne se marche plus dessus, et le démarrage de HA n'est
  plus retardé par l'intégration quand une lampe est hors de portée.
- Sous le capot, l'intégration suit maintenant un compteur interne du réseau
  mesh qu'elle supposait figé. S'il change — ça arrive sur un réseau qui vit —
  l'ancienne version serait devenue sourde **définitivement et en silence**.

Tout ça a été validé sur du vrai matériel à chaque étape, et c'est bien ce qui a
servi : les trois quarts des problèmes ci-dessus ne se voyaient pas dans les
tests, seulement en déployant.

## Pour mettre à jour

HACS puis redémarrage. Rien à refaire côté configuration, rien à recréer.

## Et toujours pas de couleur

Mon matériel est en blanc variable uniquement, donc je n'ai toujours pas livré
de RGB — je préfère ne rien livrer plutôt que du non testé. **Si vous avez une
lampe mesh couleur, ou un luminaire ThingOS d'une autre marque que Häfele, je
suis preneur** : c'est un ajout propre, il me manque juste de quoi le valider.
Même chose pour les retours de bug, même si vous n'y comprenez rien : les
diagnostics font désormais le gros du travail à votre place. 🙌

👉 https://github.com/dasimon135/ha-bluetooth-mesh
