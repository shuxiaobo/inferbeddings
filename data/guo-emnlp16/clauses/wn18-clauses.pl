_member_of_domain_topic(Y,X) :- _synset_domain_topic_of(X,Y)
_synset_domain_usage_of(Y,X) :- _member_of_domain_usage(X,Y)
_instance_hyponym(Y,X) :- _instance_hypernym(X,Y)
_hyponym(Y,X) :- _hypernym(X,Y)
_member_holonym(Y,X) :- _member_meronym(X,Y)
_synset_domain_region_of(Y,X) :- _member_of_domain_region(X,Y)
_part_of(Y,X) :- _has_part(X,Y)
_member_meronym(Y,X) :- _member_holonym(X,Y)
_hypernym(Y,X) :- _hyponym(X,Y)
_synset_domain_topic_of(Y,X) :- _member_of_domain_topic(X,Y)
_instance_hypernym(Y,X) :- _instance_hyponym(X,Y)
_has_part(Y,X) :- _part_of(X,Y)
_member_of_domain_region(Y,X) :- _synset_domain_region_of(X,Y)
_member_of_domain_usage(Y,X) :- _synset_domain_usage_of(X,Y)
