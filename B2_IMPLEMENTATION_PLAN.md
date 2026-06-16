# Plan d'implémentation — B2 Connexion Modbus persistante

## Introduction

La piste B2 vise à remplacer le schéma actuel `connect()` / transaction / `close()` répété à chaque cycle de polling, write MQTT et futur get MQTT par une connexion Modbus réutilisée entre les opérations.

L'objectif n'est pas uniquement de « garder une socket ouverte ». L'objectif réel est d'introduire une gestion explicite du cycle de vie Modbus : état de connexion, reconnexion contrôlée, backoff non bloquant, diagnostics et fermeture propre. Sans cette discipline, une connexion persistante peut devenir un point de blocage plus dangereux que le comportement actuel.

### Avantages recherchés

- Réduire la latence de chaque cycle de polling en évitant l'ouverture/fermeture systématique du transport et les délais inter-pollers excessifs.
- Accélérer les writes MQTT et les futurs getters B1, surtout si plusieurs commandes arrivent entre deux cycles.
- Diminuer le churn sur les transports TCP et série : sockets, handles de port série, handshakes, logs parasites.
- Préparer un modèle plus robuste pour les opérations à la demande, sans rendre nécessaire un refactor async complet.
- Améliorer l'observabilité : état de connexion Modbus explicite, compteur d'échecs, dernière erreur, date de prochain retry.

### Risques principaux

- Sur RTU/série, perte du « reset » implicite obtenu aujourd'hui par `close()` à chaque cycle.
- Port série réservé plus longtemps, ce qui rend le debug externe moins pratique.
- Connexion TCP half-open : la socket paraît ouverte mais la prochaine transaction bloque ou échoue.
- Backoff actuel trop couplé à `connect()` : si la connexion reste ouverte mais que le bus est down, les échecs peuvent se répéter sans cadence maîtrisée.
- Risque de transformer une opération Modbus lente en blocage de la boucle principale si les timeouts et retries ne sont pas bornés.

### Approche sélectionnée

Créer un gestionnaire de connexion Modbus synchrone, minimal et explicite, utilisé par le polling, les writes MQTT et les futurs gets MQTT.

Principes retenus :

- Une seule porte d'entrée pour toute transaction Modbus : `connection_manager.execute(...)`.
- Backoff non bloquant : si la prochaine tentative est différée, l'appel échoue rapidement au lieu de dormir longtemps dans la boucle principale.
- Délai inter-pollers adapté au transport : `0.0` pour TCP/UDP ; pour série/RTU, calcul depuis le débit (`--serial-baud`) sur la base du silence Modbus RTU de 3,5 caractères, avec plancher pratique, toujours surchargeable par `--interval`.
- Reconnexion sur échec transactionnel : fermer le transport, marquer l'état en échec, planifier un retry.
- Retry borné : au maximum une reconnexion opportuniste par opération, jamais de boucle infinie.
- Timeouts conservés au niveau pymodbus via `args.timeout`, avec budget logique supplémentaire pour éviter les commandes MQTT interminables.
- Développement dans une branche dédiée, avec tests extensifs, puis merge lorsque le nouveau modèle est stable. Le comportement persistant devient le comportement cible, sans maintenir durablement deux chemins runtime.

## État cible

### États de connexion

Le gestionnaire maintient un état parmi :

- `DISCONNECTED` : aucun transport ouvert.
- `CONNECTING` : tentative de connexion en cours.
- `READY` : transport ouvert et dernière transaction réussie ou aucune transaction échouée depuis la connexion.
- `BACKOFF` : échec récent, prochaine tentative interdite jusqu'à `backoff_until`.
- `CLOSING` : fermeture explicite en cours, principalement à l'arrêt.

### Garanties anti-blocage

- Aucun backoff long ne doit être implémenté via `sleep()` dans le chemin critique.
- Toute transaction Modbus est bornée par le timeout client existant.
- Une opération MQTT `set` ou `get` ne tente pas de reconnecter indéfiniment.
- Une connexion considérée douteuse est fermée avant de passer en backoff.
- Les diagnostics doivent permettre de distinguer :
  - pas encore connecté ;
  - connecté et sain ;
  - échec de connexion ;
  - échec de transaction ;
  - retry différé par backoff.

## Phases d'implémentation

### Phase 1 — Préparer la branche dédiée

Fichiers concernés :

- Git

Actions :

1. Créer une branche dédiée, par exemple `feature/persistent-modbus-connection`.
2. Garder des commits petits et réversibles :
   - gestionnaire de connexion seul ;
   - adaptation polling ;
   - adaptation writes/gets ;
   - diagnostics ;
   - tests et documentation.
3. Ne pas ajouter de flag de rollout `--modbus-persistent` : le nouveau modèle est testé comme comportement cible.
4. Ajouter uniquement les options de sécurité réellement utiles :
   - `--modbus-backoff-base`, défaut `1.0`.
   - `--modbus-backoff-max`, défaut `60.0`.
   - `--modbus-max-connection-age`, défaut désactivé.
5. Ajuster le défaut de `--interval` selon le transport : `0.0` pour TCP/UDP ; pour série/RTU, calculer le délai depuis `--serial-baud` avec la règle RTU des 3,5 caractères de silence et un plancher pratique.
6. Documenter clairement que le nouveau comportement réserve le port série tant que le processus tourne.

Critères d'acceptation :

- Le travail est isolé dans une branche dédiée.
- Le code n'a pas deux chemins permanents `ancien mode` / `mode persistant`.
- Les options ajoutées concernent uniquement la sécurité opérationnelle, pas le rollout.

### Phase 2 — Introduire `ModbusConnectionManager`

Fichiers concernés :

- nouveau `modpoll/modbus_connection.py`
- `modpoll/modbus_task.py`
- `modpoll/main.py`

Responsabilités du gestionnaire :

- Stocker le client pymodbus existant.
- Exposer `ensure_connected(now) -> bool`.
- Exposer `execute(operation_name, callback) -> TransactionResult`.
- Exposer `close(reason)`.
- Suivre :
  - `state`
  - `connected_since`
  - `last_success_at`
  - `last_failure_at`
  - `last_error`
  - `consecutive_failures`
  - `backoff_until`
  - `connect_count`
  - `reconnect_count`
  - `transaction_failure_count`

Comportement attendu :

1. Si `state == READY`, exécuter la transaction.
2. Si la transaction réussit, remettre `consecutive_failures` à zéro.
3. Si la transaction lève `OSError` ou `ModbusException`, fermer le client, enregistrer l'échec et passer en backoff.
4. Si la transaction retourne une réponse Modbus en erreur, traiter comme un échec transactionnel pour le polling concerné, mais ne pas forcément reconnecter immédiatement sauf politique configurable.
5. Si `state == BACKOFF` et `now < backoff_until`, échouer vite.
6. Si `state == BACKOFF` et `now >= backoff_until`, tenter une reconnexion.

Critères d'acceptation :

- Le backoff ne bloque pas la boucle principale.
- Le gestionnaire ferme systématiquement le client après une exception transport.
- Les erreurs sont disponibles pour les diagnostics.

### Phase 3 — Adapter le polling

Fichiers concernés :

- `modpoll/main.py`
- `modpoll/modbus_task.py`

Actions :

1. Remplacer l’ancien bloc `connect` / `poll` / `close` par une transaction gérée.
2. Ne plus fermer le client après chaque cycle.
3. En cas d'échec de connexion ou de backoff actif, appeler `on_poll_unavailable()` sur les handlers pour conserver les compteurs et le comportement `autoremove`.
4. Fermer le client uniquement à l'arrêt du processus.

Point d'attention :

- Le mode actuel met à jour `last_modbus_ok` après `connect()`. En mode persistant, cette variable doit représenter la santé de la dernière transaction ou l'état `READY`, pas seulement le succès de `connect()`.

Critères d'acceptation :

- Un cycle réussi ne déclenche pas de close.
- Un bus indisponible ne bloque pas plusieurs cycles par backoff dormant.
- Les publications diagnostics continuent à refléter les devices en échec.

### Phase 4 — Adapter writes MQTT et getters B1

Fichiers concernés :

- `modpoll/main.py`
- `modpoll/reference_write.py`
- `modpoll/reference_read.py`

Actions :

1. Faire passer les writes MQTT par `connection_manager.execute(...)`.
2. Faire passer les getters B1 par la même porte d'entrée lorsque B1 est présent.
3. En cas de backoff actif, refuser rapidement la commande et incrémenter les compteurs d'échec existants.
4. Appliquer une politique de retry courte :
   - si l'opération échoue par transport, fermer ;
   - si le retry est immédiatement autorisé, reconnecter une fois ;
   - sinon échouer et laisser le prochain cycle retenter.

Critères d'acceptation :

- Une rafale MQTT ne provoque pas une rafale connect/close.
- Une commande MQTT ne peut pas rester bloquée au-delà du timeout Modbus plus un petit overhead local.
- Les compteurs `setErrors`, `getErrors`, `getReadErrors` restent cohérents.

### Phase 5 — Diagnostics

Fichiers concernés :

- `modpoll/modbus_task.py`
- éventuellement `modpoll/modbus_connection.py`

Ajouter au diagnostic global `modpoll/diagnostics` :

- `modbus_connection_state`
- `modbus_connected`
- `modbus_connected_since`
- `modbus_last_success_at`
- `modbus_last_failure_at`
- `modbus_last_error`
- `modbus_consecutive_failures`
- `modbus_backoff_until`
- `modbus_connect_count`
- `modbus_reconnect_count`
- `modbus_transaction_failure_count`

Critères d'acceptation :

- Quand le bus est down, on voit explicitement si le processus attend un retry.
- Quand le bus revient, les compteurs et l'état reviennent à `READY`.
- Les diagnostics restent publiés même lorsque Modbus est en backoff.

### Phase 6 — Tests unitaires

Fichiers concernés :

- tests existants à identifier
- nouveaux tests dédiés au gestionnaire

Cas à couvrir :

1. Connexion initiale réussie.
2. Connexion initiale échouée puis backoff non bloquant.
3. Backoff expiré puis reconnexion réussie.
4. Exception `OSError` pendant une transaction : fermeture, backoff, erreur remontée.
5. Exception `ModbusException` pendant une transaction : même comportement.
6. Transaction réussie après échecs : reset de `consecutive_failures`.
7. `close()` idempotent à l'arrêt.
8. Mode non persistant inchangé.

Critères d'acceptation :

- Les tests n'utilisent pas de vrai port série ni de vraie socket.
- Les sleeps sont évités via injection de temps ou horloge contrôlée.
- Le backoff est vérifié par timestamps, pas par attente réelle.

### Phase 7 — Tests d'intégration simulés

Objectif :

- Valider l'enchaînement `main.py` sans dépendre d'un équipement Modbus réel.

Scénarios :

1. Poll normal sur client mocké : un seul `connect()` pour plusieurs cycles persistants.
2. Write MQTT après poll : réutilisation de la connexion.
3. Échec transport pendant poll : close immédiat, backoff, cycle suivant skip rapide.
4. Retour du bus : reconnexion et reprise des polls.
5. Arrêt processus : close appelé une fois.

Critères d'acceptation :

- Le nombre d'appels `connect()` / `close()` démontre la persistance.
- Les cycles en backoff restent courts.
- Les compteurs diagnostics progressent de manière déterministe.

### Phase 8 — Documentation et validation avant merge

Fichiers concernés :

- `ROADMAP.md`
- `docs/usage.rst`
- changelog si présent

Actions :

1. Documenter la connexion persistante comme nouveau comportement.
2. Expliquer les différences RTU/TCP.
3. Ajouter une section dépannage :
   - port série déjà utilisé ;
   - bus down mais process vivant ;
   - backoff en cours ;
   - équipement qui nécessite une reconnexion périodique.
4. Mettre à jour `ROADMAP.md` avec la décision d'architecture si le chantier est livré.

Validation avant merge :

- Suite unitaire complète.
- Tests d'intégration simulés.
- Test manuel TCP si un simulateur est disponible.
- Test manuel RTU ou pyserial URL simulée si possible.
- Revue spécifique des scénarios de blocage : bus down, device muet, socket half-open, arrêt processus.
- Merge uniquement lorsque le nouveau comportement est prouvé stable.

## Politique de reconnexion recommandée

### Échec de connexion

- Incrémenter `consecutive_failures`.
- Calculer `backoff_until = now + min(base * 2 ** (failures - 1), max)`.
- Ne pas dormir dans la fonction.
- Retourner un échec rapide à l'appelant.

### Échec de transaction transport

- Fermer immédiatement le client.
- Marquer l'état `BACKOFF`.
- Enregistrer `last_error`.
- Laisser les compteurs métier existants marquer l'opération comme échouée.

### Réponse Modbus en erreur

- Ne pas reconnecter automatiquement dans tous les cas : une exception Modbus applicative peut venir du device, de l'adresse ou de la fonction.
- Pour le polling, laisser `Poller.poll()` marquer l'échec.
- Pour les writes/gets, incrémenter les compteurs existants.
- Option future : politique configurable pour reconnecter après N réponses en erreur consécutives.

### Recyclage préventif

Ajouter seulement si nécessaire après tests terrain :

- `--modbus-max-connection-age` pour fermer/reconnecter après une durée donnée.
- `--modbus-recycle-after-failures` pour forcer un reset après N échecs applicatifs.
- `--modbus-recycle-idle-after` pour fermer une connexion inactive longtemps.

## Points à ne pas faire

- Ne pas remplacer le problème par une boucle de retry infinie.
- Ne pas faire dormir la boucle principale pendant 30 ou 60 secondes de backoff.
- Ne pas maintenir durablement deux chemins runtime par flag de rollout.
- Ne pas introduire d'async dans ce chantier : B2 doit rester une amélioration ciblée.
- Ne pas masquer les erreurs MQTT : une commande refusée par backoff doit être comptée et visible dans les diagnostics.

## Définition de terminé

B2 est terminé lorsque :

- Polling, writes MQTT et getters utilisent la même gestion de connexion.
- Une panne Modbus ne bloque pas durablement la boucle principale.
- Le délai inter-pollers par défaut ne masque pas le gain de la connexion persistante, en particulier sur TCP.
- Les échecs transport déclenchent close + backoff + diagnostic ; les réponses d'exception Modbus applicatives restent comptées comme erreurs d'opération sans fermeture immédiate.
- Les tests couvrent connexion, reconnexion, backoff, transaction réussie, transaction échouée et fermeture.
- La branche dédiée peut être mergée sans flag de rollout persistant.
