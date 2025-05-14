# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Logical Replication implementation.

TODO: add description after specification is accepted.
"""

import json
import logging

from ops import (
    BlockedStatus,
    EventBase,
    LeaderElectedEvent,
    Object,
    Relation,
    RelationBrokenEvent,
    RelationChangedEvent,
    RelationDepartedEvent,
    RelationJoinedEvent,
    Secret,
    SecretChangedEvent,
    SecretNotFoundError,
)

from cluster_topology_observer import ClusterTopologyChangeEvent
from utils import new_password

logger = logging.getLogger(__name__)

LOGICAL_REPLICATION_OFFER_RELATION = "logical-replication-offer"
LOGICAL_REPLICATION_RELATION = "logical-replication"
SECRET_LABEL = "logical-replication-relation"  # noqa: S105
LOGICAL_REPLICATION_VALIDATION_ERROR_STATUS = "Logical replication setup is invalid. Check logs"


class PostgreSQLLogicalReplication(Object):
    """Defines the logical-replication logic."""

    def __init__(self, charm):
        super().__init__(charm, "postgresql_logical_replication")
        self.charm = charm
        # Relations
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_OFFER_RELATION].relation_joined,
            self._on_offer_relation_joined,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_OFFER_RELATION].relation_changed,
            self._on_offer_relation_changed,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_OFFER_RELATION].relation_departed,
            self._on_offer_relation_departed,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_OFFER_RELATION].relation_broken,
            self._on_offer_relation_broken,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_RELATION].relation_joined, self._on_relation_joined
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_RELATION].relation_changed, self._on_relation_changed
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_RELATION].relation_departed,
            self._on_relation_departed,
        )
        self.charm.framework.observe(
            self.charm.on[LOGICAL_REPLICATION_RELATION].relation_broken, self._on_relation_broken
        )
        # Events
        self.charm.framework.observe(
            self.charm.on.cluster_topology_change, self._on_cluster_topology_change
        )
        self.charm.framework.observe(
            self.charm.on.leader_elected, self._on_cluster_topology_change
        )
        self.framework.observe(self.charm.on.secret_changed, self._on_secret_changed)

    # region Relations

    def _on_offer_relation_joined(self, event: RelationJoinedEvent):
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_OFFER_RELATION} join deferred until primary is available"
            )
            return

        secret = self._get_secret(event.relation.id)
        secret.grant(event.relation)
        event.relation.data[self.model.app]["secret-id"] = secret.id

    def _on_offer_relation_changed(self, event: RelationChangedEvent):
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_OFFER_RELATION} change deferred until primary is available"
            )
            return
        self._process_offer(event.relation)

    def _on_offer_relation_departed(self, event: RelationDepartedEvent):
        if event.departing_unit == self.charm.unit and self.charm._peers is not None:
            self.charm.unit_peer_data.update({"departing": "True"})

    def _on_offer_relation_broken(self, event: RelationBrokenEvent):
        if not self.charm._peers or self.charm.is_unit_departing:
            logger.debug(
                f"{LOGICAL_REPLICATION_OFFER_RELATION} break skipped due to departing unit"
            )
            return
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_OFFER_RELATION} break deferred until primary is available"
            )
            return

        # TODO: fix empty relation data after defer
        publications = json.loads(event.relation.data[self.model.app].get("publications", "{}"))
        for database, publication in publications.items():
            self.charm.postgresql.drop_publication(database, publication["publication-name"])

        secret = self._get_secret(event.relation.id)
        self.charm.postgresql.delete_user(secret.peek_content()["username"])
        secret.remove_all_revisions()

        self.charm.update_config()

    def _on_relation_joined(self, event: RelationJoinedEvent):
        if not self.charm.unit.is_leader():
            return
        if self.charm.app_peer_data.get("logical-replication-validation") == "ongoing":
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_RELATION} join deferred due to validation is still ongoing"
            )
            return
        if self.charm.app_peer_data.get("logical-replication-validation") == "error":
            logger.debug(
                f"{LOGICAL_REPLICATION_OFFER_RELATION} join skipped due to validation error"
            )
            return
        event.relation.data[self.model.app]["subscription-request"] = (
            self.charm.config.logical_replication_subscription_request or ""
        )

    def _on_relation_changed(self, event: RelationChangedEvent):
        if not self._relation_changed_checks(event):
            return

        secret_content = self.model.get_secret(
            id=event.relation.data[event.app]["secret-id"]
        ).get_content(refresh=True)
        subscriptions = json.loads(
            self.charm.app_peer_data.get("logical-replication-subscriptions", "{}")
        )
        publications = json.loads(event.relation.data[event.app].get("publications", "{}"))

        for database, publication in publications.items():
            subscription_name = self._subscription_name(event.relation.id, database)
            if database in subscriptions:
                self.charm.postgresql.refresh_subscription(database, subscription_name)
                logger.debug(
                    f"Refreshed subscription {subscription_name} in database {database} due to relation change"
                )
            else:
                publication_name = publication["publication-name"]
                self.charm.postgresql.create_subscription(
                    subscription_name,
                    secret_content["primary"],
                    database,
                    secret_content["username"],
                    secret_content["password"],
                    publication_name,
                    publication["replication-slot-name"],
                )
                logger.debug(
                    f"Created new subscription {subscription_name} for publication {publication_name} in database {database}"
                )
                subscriptions[database] = subscription_name

        for database, subscription in subscriptions.copy().items():
            if database in publications:
                continue
            self.charm.postgresql.drop_subscription(database, subscription)
            logger.debug(f"Dropped redundant subscription {subscription} from database {database}")
            del subscriptions[database]

        self.charm.app_peer_data["logical-replication-subscriptions"] = json.dumps(subscriptions)

    def _on_relation_departed(self, event: RelationDepartedEvent):
        if event.departing_unit == self.charm.unit and self.charm._peers is not None:
            self.charm.unit_peer_data.update({"departing": "True"})

    def _on_relation_broken(self, event: RelationBrokenEvent):
        if not self.charm._peers or self.charm.is_unit_departing:
            logger.debug(f"{LOGICAL_REPLICATION_RELATION} break skipped due to departing unit")
            return
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_RELATION} break deferred until primary is available"
            )
            return

        subscriptions = json.loads(
            self.charm.app_peer_data.get("logical-replication-subscriptions", "{}")
        )
        for database, subscription in subscriptions.items():
            self.charm.postgresql.drop_subscription(database, subscription)
            logger.debug(
                f"Dropped subscription {subscriptions} from database {database} due to relation break"
            )
        self.charm.app_peer_data["logical-replication-subscriptions"] = ""

    # endregion

    # region Events

    def _on_cluster_topology_change(self, event: ClusterTopologyChangeEvent | LeaderElectedEvent):
        if not self.charm.unit.is_leader():
            return
        if not len(self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ())):
            return
        if not self.charm.primary_endpoint:
            event.defer()
            return
        for relation in self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ()):
            self._get_secret(relation.id)

    def _on_secret_changed(self, event: SecretChangedEvent):
        if not self.charm.unit.is_leader():
            return
        if not self.charm.primary_endpoint:
            event.defer()
            return

        if (
            relation := self.model.get_relation(LOGICAL_REPLICATION_RELATION)
        ) and event.secret.label.startswith(SECRET_LABEL):
            logger.info("Logical replication secret changed, updating subscriptions")
            secret_content = self.model.get_secret(
                id=relation.data[relation.app]["secret-id"], label=SECRET_LABEL
            ).get_content(refresh=True)
            subscriptions = json.loads(
                self.charm.app_peer_data.get("logical-replication-subscriptions", "{}")
            )
            for database, subscription in subscriptions.items():
                self.charm.postgresql.update_subscription(
                    database,
                    subscription,
                    secret_content["primary"],
                    secret_content["username"],
                    secret_content["password"],
                )

    # endregion

    def apply_changed_config(self, event: EventBase) -> bool:
        """Validate & apply (relation) logical_replication_subscription_request config parameter."""
        if not self.charm.unit.is_leader():
            return True
        if not self.charm.primary_endpoint:
            self.charm.app_peer_data["logical-replication-validation"] = "ongoing"
            event.defer()
            return False
        if self._validate_subscription_request():
            self._apply_updated_subscription_request()
        return True

    def retry_validations(self):
        """Run recurrent logical replication validation attempt.

        For subscribers - try to validate & apply subscription request.
        For publishers - try to validate & process all the offer relations.
        """
        if not self.charm.unit.is_leader() or not self.charm.primary_endpoint:
            return
        if (
            self.charm.app_peer_data.get("logical-replication-validation") == "error"
            and self._validate_subscription_request()
        ):
            self._apply_updated_subscription_request()
        for relation in self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ()):
            if json.loads(relation.data[self.model.app].get("errors", "[]")):
                self._process_offer(relation)

    def replication_slots(self) -> dict[str, str]:
        """Get list of all managed replication slots.

        Returns: dictionary in <slot>: <database> format.
        """
        return {
            publication["replication-slot-name"]: database
            for relation in self.model.relations.get(LOGICAL_REPLICATION_OFFER_RELATION, ())
            for database, publication in json.loads(
                relation.data[self.model.app].get("publications", "{}")
            ).items()
        }

    def _apply_updated_subscription_request(self):
        if not (relation := self.model.get_relation(LOGICAL_REPLICATION_RELATION)):
            return
        # TODO: cleanup
        relation.data[self.model.app]["subscription-request"] = (
            self.charm.config.logical_replication_subscription_request
        )

    def _validate_subscription_request(self) -> bool:
        try:
            subscription_request_config = json.loads(
                self.charm.config.logical_replication_subscription_request or "{}"
            )
        except json.JSONDecodeError as err:
            return self._fail_validation(f"JSON decode error {err}")

        relation = self.model.get_relation(LOGICAL_REPLICATION_RELATION)
        subscription_request_relation = (
            json.loads(relation.data[self.model.app].get("subscription-request", "{}"))
            if relation
            else {}
        )

        for database, schematables in subscription_request_config.items():
            if not self.charm.postgresql.database_exists(database):
                return self._fail_validation(f"database {database} doesn't exist")
            for schematable in schematables:
                try:
                    schema, table = schematable.split(".")
                except ValueError:
                    return self._fail_validation(f"table format isn't right at {schematable}")
                if not self.charm.postgresql.table_exists(database, schema, table):
                    return self._fail_validation(
                        f"table {schematable} in database {database} doesn't exist"
                    )
                already_subscribed = (
                    database in subscription_request_relation
                    and schematable in subscription_request_relation[database]
                )
                if not already_subscribed and not self.charm.postgresql.is_table_empty(
                    database, schema, table
                ):
                    return self._fail_validation(
                        f"table {schematable} in database {database} isn't empty"
                    )

        self.charm.app_peer_data["logical-replication-validation"] = ""
        return True

    def _fail_validation(self, message: str | None = None) -> bool:
        if message:
            logger.error(f"Logical replication validation: {message}")
        self.charm.app_peer_data["logical-replication-validation"] = "error"
        self.charm.unit.status = BlockedStatus(LOGICAL_REPLICATION_VALIDATION_ERROR_STATUS)
        return False

    def _validate_new_publication(
        self,
        database: str,
        schematables: list[str],
        publication_schematables: list[str] | None = None,
    ) -> str | None:
        if not self.charm.postgresql.database_exists(database):
            return f"database {database} doesn't exist"
        for schematable in schematables:
            if publication_schematables is not None and schematable in publication_schematables:
                continue
            schema, table = schematable.split(".")
            if not self.charm.postgresql.table_exists(database, schema, table):
                return f"table {schematable} in database {database} doesn't exist"
        return None

    def _relation_changed_checks(self, event: RelationChangedEvent) -> bool:
        if not self.charm.unit.is_leader():
            return False
        if not event.relation.data[event.app].get("secret-id"):
            logger.warning(
                f"{LOGICAL_REPLICATION_RELATION} change skipped due to secret absence in remote application data"
            )
            return False
        if not self.charm.primary_endpoint:
            event.defer()
            logger.debug(
                f"{LOGICAL_REPLICATION_RELATION} change deferred until primary is available"
            )
            return False
        return True

    def _process_offer(self, relation: Relation):
        subscriptions_request = json.loads(
            relation.data[relation.app].get("subscription-request", "{}")
        )
        publications = json.loads(relation.data[self.model.app].get("publications", "{}"))
        user = self._get_secret(relation.id).peek_content()["username"]
        errors = []

        for database, publication in publications.copy().items():
            if database in subscriptions_request:
                continue
            self.charm.postgresql.drop_publication(database, publication["publication-name"])
            logger.debug(
                f"Dropped redundant publication {publication['publication-name']} from database {database}"
            )
            del publications[database]
            self.charm.postgresql.revoke_replication_privileges(
                user, database, publication["tables"]
            )
            logger.debug(f"Revoked replication privileges on database {database} from user {user}")

        for database, tables in subscriptions_request.items():
            if database not in publications:
                if validation_error := self._validate_new_publication(database, tables):
                    errors.append(validation_error)
                    logger.debug(f"Cannot create new publication: {validation_error}")
                    continue
                self.charm.postgresql.grant_replication_privileges(user, database, tables)
                logger.debug(
                    f"Granted replication privileges on database {database} for user {user}"
                )
                publication_name = self._publication_name(relation.id, database)
                self.charm.postgresql.create_publication(database, publication_name, tables)
                logger.debug(
                    f"Created new publication {publication_name} for tables {', '.join(tables)} in database {database}"
                )
                publications[database] = {
                    "publication-name": publication_name,
                    "replication-slot-name": self._replication_slot_name(relation.id, database),
                    "tables": tables,
                }
            elif sorted(publication_tables := publications[database]["tables"]) != sorted(tables):
                publication_name = publications[database]["publication-name"]
                if validation_error := self._validate_new_publication(
                    database, tables, publication_tables
                ):
                    errors.append(validation_error)
                    logger.debug(
                        f"Cannot alter publication {publication_name}: {validation_error}"
                    )
                    continue
                self.charm.postgresql.grant_replication_privileges(
                    user, database, tables, publication_tables
                )
                logger.debug(
                    f"Altered replication privileges on database {database} for user {user}"
                )
                self.charm.postgresql.alter_publication(database, publication_name, tables)
                logger.debug(
                    f"Altered publication {publication_name} tables from {','.join(publication_tables)} to {','.join(tables)} in database {database}"
                )
                publications[database]["tables"] = tables

        relation.data[self.model.app].update({
            "errors": json.dumps(errors),
            "publications": json.dumps(publications),
        })
        self.charm.update_config()

    def _publication_name(self, relation_id: int, database: str):
        return f"relation_{relation_id}_{database}"

    def _replication_slot_name(self, relation_id: int, database: str):
        return f"relation_{relation_id}_{database}"

    def _subscription_name(self, relation_id: int, database: str):
        return f"relation_{relation_id}_{database}"

    def _create_user(self, relation_id: int) -> tuple[str, str]:
        user = f"logical-replication-relation-{relation_id}"
        password = new_password()
        self.charm.postgresql.create_user(user, password, replication=True)
        return user, password

    def _get_secret(self, relation_id: int) -> Secret:
        """Returns logical replication secret. Updates, if content changed."""
        secret_label = f"{SECRET_LABEL}-{relation_id}"
        primary = self.charm.primary_endpoint
        try:
            # Avoid recreating the secret.
            secret = self.charm.model.get_secret(label=secret_label)
            if not secret.id:
                # Workaround for the secret id not being set with model uuid.
                secret._id = f"secret://{self.model.uuid}/{secret.get_info().id.split(':')[1]}"
            if (content := secret.peek_content())["primary"] != primary:
                logger.info("Updating outdated secret content")
                content["primary"] = primary
                secret.set_content(content)
            return secret
        except SecretNotFoundError:
            logger.debug("Secret not found, creating a new one")
            pass
        username, password = self._create_user(relation_id)
        return self.charm.model.app.add_secret(
            content={
                "primary": primary,
                "username": username,
                "password": password,
            },
            label=secret_label,
        )
